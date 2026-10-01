//! GPU color conversion: RGBA16F (PQ-encoded Rec.2020 RGB) -> P010
//! (BT.2020 non-constant, limited range, 10-bit) via a compute shader
//! writing directly into the QSV encoder surface (UAV). Zero CPU pixels.

#![allow(dead_code)]

use windows::core::Interface as _;
use windows::Win32::Graphics::Direct3D11::{
    D3D11_RESOURCE_MISC_FLAG, D3D11_TEX2D_UAV1, D3D11_TEXTURE2D_DESC, D3D11_UAV_DIMENSION_TEXTURE2D,
    D3D11_UNORDERED_ACCESS_VIEW_DESC1, D3D11_USAGE_DEFAULT, ID3D11ComputeShader, ID3D11Device,
    ID3D11Device3, ID3D11DeviceContext, ID3D11ShaderResourceView, ID3D11Texture2D,
    ID3D11UnorderedAccessView, ID3D11UnorderedAccessView1,
};
use windows::Win32::Graphics::Dxgi::Common::{
    DXGI_FORMAT_B8G8R8A8_UNORM, DXGI_FORMAT_P010, DXGI_FORMAT_R16_UINT, DXGI_FORMAT_R16G16_UINT,
    DXGI_SAMPLE_DESC,
};

use crate::d3d::{blob_bytes, compile};

const CS: &str = r#"
Texture2D<float4> tScene : register(t0);      // RGBA16F, PQ-encoded RGB
RWTexture2D<uint>  uY    : register(u0);      // P010 plane 0 (R16_UINT view)
RWTexture2D<uint2> uUV   : register(u1);      // P010 plane 1 (R16G16_UINT view)

[numthreads(16, 16, 1)]
void main(uint3 dtid : SV_DispatchThreadID) {
    int2 p = int2(dtid.xy) * 2;
    float3 c0 = tScene.Load(int3(p + int2(0, 0), 0)).rgb;
    float3 c1 = tScene.Load(int3(p + int2(1, 0), 0)).rgb;
    float3 c2 = tScene.Load(int3(p + int2(0, 1), 0)).rgb;
    float3 c3 = tScene.Load(int3(p + int2(1, 1), 0)).rgb;

    // BT.2020 non-constant luminance matrix
    float y0 = 0.2627 * c0.r + 0.6780 * c0.g + 0.0593 * c0.b;
    float y1 = 0.2627 * c1.r + 0.6780 * c1.g + 0.0593 * c1.b;
    float y2 = 0.2627 * c2.r + 0.6780 * c2.g + 0.0593 * c2.b;
    float y3 = 0.2627 * c3.r + 0.6780 * c3.g + 0.0593 * c3.b;

    // limited range 10-bit: Y = 64 + 876*Y', C = 512 + 896*C'
    uint Y0 = uint(clamp(64.0 + 876.0 * y0, 0.0, 1023.0));
    uint Y1 = uint(clamp(64.0 + 876.0 * y1, 0.0, 1023.0));
    uint Y2 = uint(clamp(64.0 + 876.0 * y2, 0.0, 1023.0));
    uint Y3 = uint(clamp(64.0 + 876.0 * y3, 0.0, 1023.0));

    float cb0 = (c0.b - y0) / 1.8814;
    float cr0 = (c0.r - y0) / 1.4746;
    float cb1 = (c1.b - y1) / 1.8814;
    float cr1 = (c1.r - y1) / 1.4746;
    float cb2 = (c2.b - y2) / 1.8814;
    float cr2 = (c2.r - y2) / 1.4746;
    float cb3 = (c3.b - y3) / 1.8814;
    float cr3 = (c3.r - y3) / 1.4746;
    float cb = 0.25 * (cb0 + cb1 + cb2 + cb3);
    float cr = 0.25 * (cr0 + cr1 + cr2 + cr3);
    uint U = uint(clamp(512.0 + 896.0 * cb, 0.0, 1023.0));
    uint V = uint(clamp(512.0 + 896.0 * cr, 0.0, 1023.0));

    // P010: 10-bit values in the high bits of 16-bit words
    uY[p + int2(0, 0)] = Y0 << 6;
    uY[p + int2(1, 0)] = Y1 << 6;
    uY[p + int2(0, 1)] = Y2 << 6;
    uY[p + int2(1, 1)] = Y3 << 6;
    uUV[dtid.xy] = uint2(U << 6, V << 6);
}
"#;

pub struct Converter {
    pub cs: ID3D11ComputeShader,
}

impl Converter {
    pub fn new(device: &ID3D11Device) -> Result<Self, String> {
        unsafe {
            let blob = compile(CS, "main", "cs_5_0")?;
            let bytes = blob_bytes(&blob).to_vec();
            let mut cs: Option<ID3D11ComputeShader> = None;
            device
                .CreateComputeShader(&bytes, None, Some(&mut cs))
                .map_err(|e| format!("CreateComputeShader: {e}"))?;
            Ok(Converter { cs: cs.ok_or("cs null")? })
        }
    }

    /// Create the P010 encoder surface texture. Bind flags match FFmpeg's
    /// working d3d11va hwupload->qsv path (SRV|UAV, no DECODER/VIDEO_ENCODER
    /// binds): the app hands the runtime an external texture via MemId.
    pub fn create_p010_surface(
        device: &ID3D11Device,
        w: u32,
        h: u32,
    ) -> Result<ID3D11Texture2D, String> {
        unsafe {
            let desc = D3D11_TEXTURE2D_DESC {
                Width: w,
                Height: h,
                MipLevels: 1,
                ArraySize: 1,
                Format: DXGI_FORMAT_P010,
                SampleDesc: DXGI_SAMPLE_DESC { Count: 1, Quality: 0 },
                Usage: D3D11_USAGE_DEFAULT,
                BindFlags: (windows::Win32::Graphics::Direct3D11::D3D11_BIND_SHADER_RESOURCE.0
                    | windows::Win32::Graphics::Direct3D11::D3D11_BIND_UNORDERED_ACCESS.0)
                    as u32,
                CPUAccessFlags: 0,
                MiscFlags: 0,
            };
            let mut tex: Option<ID3D11Texture2D> = None;
            device
                .CreateTexture2D(&desc, None, Some(&mut tex))
                .map_err(|e| format!("create P010 surface (SRV|UAV): {e}"))?;
            Ok(tex.ok_or("tex null")?)
        }
    }

    /// UAV views over the two P010 planes using the D3D11.3 video-plane
    /// mechanism: DESC1 + TEX2D_UAV1 with PlaneSlice 0 (Y, R16_UINT) and
    /// PlaneSlice 1 (UV interleaved, R16G16_UINT).
    pub fn create_p010_uavs(
        device: &ID3D11Device,
        tex: &ID3D11Texture2D,
    ) -> Result<(ID3D11UnorderedAccessView, ID3D11UnorderedAccessView), String> {
        unsafe {
            // D3D11.3 video-plane UAVs (DESC1 + PlaneSlice) live on ID3D11Device3
            // and return ID3D11UnorderedAccessView1 objects.
            let dev3: ID3D11Device3 = device
                .cast()
                .map_err(|e| format!("cast ID3D11Device3: {e}"))?;
            let y_desc = D3D11_UNORDERED_ACCESS_VIEW_DESC1 {
                Format: DXGI_FORMAT_R16_UINT,
                ViewDimension: D3D11_UAV_DIMENSION_TEXTURE2D,
                Anonymous: windows::Win32::Graphics::Direct3D11::D3D11_UNORDERED_ACCESS_VIEW_DESC1_0 {
                    Texture2D: D3D11_TEX2D_UAV1 {
                        MipSlice: 0,
                        PlaneSlice: 0,
                    },
                },
            };
            let uv_desc = D3D11_UNORDERED_ACCESS_VIEW_DESC1 {
                Format: DXGI_FORMAT_R16G16_UINT,
                ViewDimension: D3D11_UAV_DIMENSION_TEXTURE2D,
                Anonymous: windows::Win32::Graphics::Direct3D11::D3D11_UNORDERED_ACCESS_VIEW_DESC1_0 {
                    Texture2D: D3D11_TEX2D_UAV1 {
                        MipSlice: 0,
                        PlaneSlice: 1,
                    },
                },
            };
            let mut y1: Option<ID3D11UnorderedAccessView1> = None;
            dev3
                .CreateUnorderedAccessView1(
                    tex,
                    Some(&y_desc),
                    Some(&mut y1 as *mut Option<ID3D11UnorderedAccessView1>),
                )
                .map_err(|e| format!("P010 Y-plane UAV (R16_UINT): {e}"))?;
            let mut uv1: Option<ID3D11UnorderedAccessView1> = None;
            dev3
                .CreateUnorderedAccessView1(
                    tex,
                    Some(&uv_desc),
                    Some(&mut uv1 as *mut Option<ID3D11UnorderedAccessView1>),
                )
                .map_err(|e| format!("P010 UV-plane UAV (R16G16_UINT): {e}"))?;
            let y: ID3D11UnorderedAccessView = y1
                .ok_or("y null")?
                .cast()
                .map_err(|e| format!("cast UAV1->UAV: {e}"))?;
            let uv: ID3D11UnorderedAccessView = uv1
                .ok_or("uv null")?
                .cast()
                .map_err(|e| format!("cast UAV1->UAV: {e}"))?;
            Ok((y, uv))
        }
    }

    /// Run the conversion: tScene SRV -> Y/UV UAVs on the encoder surface.
    /// Each thread covers a 2x2 luma block + one chroma pair, so the dispatch
    /// is (w/2)x(h/2) threads (e.g. 1920x1080 groups-of-16 at 4K).
    pub fn convert(
        &self,
        context: &ID3D11DeviceContext,
        scene_srv: &ID3D11ShaderResourceView,
        y_uav: &ID3D11UnorderedAccessView,
        uv_uav: &ID3D11UnorderedAccessView,
        w: u32,
        h: u32,
    ) {
        unsafe {
            context.CSSetShader(Some(&self.cs), None);
            context.CSSetShaderResources(0, Some(&[Some(scene_srv.clone())]));
            let uavs = [Some(y_uav.clone()), Some(uv_uav.clone())];
            context.CSSetUnorderedAccessViews(0, 2, Some(uavs.as_ptr()), None);
            let groups_x = (w / 2 + 15) / 16;
            let groups_y = (h / 2 + 15) / 16;
            context.Dispatch(groups_x, groups_y, 1);
            // unbind to avoid read-after-write hazards on the next frame
            let none = [None, None];
            context.CSSetUnorderedAccessViews(0, 2, Some(none.as_ptr()), None);
            context.CSSetShaderResources(0, Some(&[None]));
        }
    }
}