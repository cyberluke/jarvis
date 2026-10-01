//! Scene renderer: fullscreen pixel shader on an RGBA16F HDR target,
//! text masks rasterized once / on change with Direct2D + DirectWrite.

#![allow(dead_code)]

use windows::core::{Interface as _, PCWSTR};
use windows::Win32::Graphics::Direct2D::Common::{
    D2D1_ALPHA_MODE_PREMULTIPLIED, D2D1_COLOR_F, D2D1_PIXEL_FORMAT, D2D_RECT_F,
};
use windows::Win32::Graphics::Direct2D::{
    D2D1CreateFactory, D2D1_BITMAP_OPTIONS_TARGET, D2D1_BITMAP_PROPERTIES1,
    D2D1_DEVICE_CONTEXT_OPTIONS_NONE, D2D1_DRAW_TEXT_OPTIONS_NONE, D2D1_FACTORY_TYPE_SINGLE_THREADED,
    ID2D1Bitmap1, ID2D1Device, ID2D1DeviceContext, ID2D1Factory1, ID2D1Image, ID2D1RenderTarget, ID2D1SolidColorBrush,
};
use windows::Win32::Graphics::Direct3D::{
    D3D11_SRV_DIMENSION_TEXTURE2D, D3D_PRIMITIVE_TOPOLOGY_TRIANGLELIST,
};
use windows::Win32::Graphics::Direct3D11::{
    D3D11_BIND_CONSTANT_BUFFER, D3D11_BIND_RENDER_TARGET, D3D11_BIND_SHADER_RESOURCE,
    D3D11_BUFFER_DESC, D3D11_CPU_ACCESS_FLAG, D3D11_CPU_ACCESS_WRITE, D3D11_CULL_NONE,
    D3D11_FILL_SOLID, D3D11_MAP_WRITE_DISCARD, D3D11_RASTERIZER_DESC,
    D3D11_RENDER_TARGET_VIEW_DESC, D3D11_RESOURCE_MISC_FLAG, D3D11_SHADER_RESOURCE_VIEW_DESC,
    D3D11_TEX2D_RTV, D3D11_TEX2D_SRV, D3D11_TEXTURE2D_DESC, D3D11_USAGE_DEFAULT,
    D3D11_USAGE_DYNAMIC, D3D11_VIEWPORT, ID3D11Buffer, ID3D11Device, ID3D11DeviceContext,
    ID3D11PixelShader, ID3D11RasterizerState, ID3D11RenderTargetView, ID3D11ShaderResourceView,
    ID3D11Texture2D, ID3D11VertexShader,
};
use windows::Win32::Graphics::DirectWrite::{
    DWriteCreateFactory, DWRITE_FACTORY_TYPE_SHARED, DWRITE_FONT_STRETCH_NORMAL,
    DWRITE_FONT_STYLE_NORMAL, DWRITE_FONT_WEIGHT_NORMAL, DWRITE_MEASURING_MODE_NATURAL,
    DWRITE_TEXT_METRICS, IDWriteFactory, IDWriteTextFormat,
};
use windows::Win32::Graphics::Dxgi::Common::{DXGI_FORMAT_B8G8R8A8_UNORM, DXGI_FORMAT_R16G16B16A16_FLOAT, DXGI_SAMPLE_DESC};
use windows::Win32::Graphics::Dxgi::{IDXGIDevice, IDXGISurface};

use crate::d3d::{blob_bytes, compile, format_name};

pub const W: u32 = 3840;
pub const H: u32 = 2160;

// ── HLSL ────────────────────────────────────────────────────────────────

const VS: &str = r#"
struct VSOut { float4 pos : SV_Position; float2 uv : TEXCOORD0; };
VSOut main(uint id : SV_VertexID) {
    VSOut o;
    float2 p = float2((id << 1) & 2, id & 2);
    o.pos = float4(p * 2.0 - 1.0, 0.0, 1.0);
    o.uv = p * 0.5;
    return o;
}
"#;

const PS: &str = r#"
Texture2D<float4> tWordmark : register(t0);
Texture2D<float4> tLabel    : register(t1);
Texture2D<float4> tClock    : register(t2);
Texture2D<float4> tHud      : register(t3);
SamplerState s0 : register(s0);

cbuffer Scene : register(b0) {
    float4 u_res_t;      // xy = resolution px, z = time t, w = pad
    float4 u_clock;      // xy = pos px, zw = size px
    float4 u_hud;
    float4 u_wordmark;
    float4 u_label;
    float4 u_bg_nits;
    float4 u_wordmark_nits;
    float4 u_label_nits;
    float4 u_clock_nits;
    float4 u_hud_nits;
    float4 u_spark_nits;
    float4 u_blob_nits;
    float4 u_hairline_nits;
    float4 u_flip;
}

float pq(float n) {
    n = clamp(n / 10000.0, 0.0, 1.0);
    float m1 = 2610.0 / 16384.0;
    float m2 = 2523.0 / 32.0;
    float c1 = 3424.0 / 4096.0;
    float c2 = 2413.0 / 128.0;
    float c3 = 2392.0 / 128.0;
    float ym = pow(n, m1);
    return pow((c1 + c2 * ym) / (1.0 + c3 * ym), m2);
}
float3 pq3(float3 nits) { return float3(pq(nits.x), pq(nits.y), pq(nits.z)); }

float mask(Texture2D<float4> tex, float4 ps, float2 frag) {
    float2 p = (frag - ps.xy) / max(ps.zw, float2(1.0, 1.0));
    if (p.x < 0.0 || p.x > 1.0 || p.y < 0.0 || p.y > 1.0) return 0.0;
    p.y = 1.0 - p.y;
    return tex.Sample(s0, p).r;
}

struct PSIn { float4 pos : SV_Position; float2 uv : TEXCOORD0; };

float4 main(PSIn i) : SV_Target {
    float2 px = i.pos.xy;
    // Box compatibility: the Amlogic 4K HEVC video plane vertically flips this
    // encoder's stream (proven against ffmpeg hevc_qsv, which does not flip).
    // Pre-flip the source so the box displays it right side up. The correction
    // is scoped to the Homatics live-video target (verticalPreFlip=true); local
    // file encodes default to no flip. Explicit override wins:
    //   u_flip.x == 1.0  VERTICAL_FLIP  (top<->bottom)
    //   u_flip.x == 2.0  ROTATE_180     (both axes — a different correction)
    if (u_flip.x == 1.0) px.y = u_res_t.y - 1.0 - px.y;
    else if (u_flip.x == 2.0) px = float2(u_res_t.x - 1.0 - px.x, u_res_t.y - 1.0 - px.y);
    float3 c = pq3(u_bg_nits.xyz);

    // 1 px diagnostic hairlines (physical pixels)
    if (px.y < 1.0 || px.x < 1.0) c = pq3(u_hairline_nits.xyz);
    float xi = floor(px.x), yi = floor(px.y);
    if (yi >= 24.0 && yi < 120.0 && xi >= 24.0 && xi < 120.0 && (int(xi) - 24) % 2 == 0)
        c = pq3(u_hairline_nits.xyz);

    // dark-to-bright PQ ramp (0.02 -> 400 nits over cols 320..3520)
    if (yi >= 1960.0 && yi < 2040.0 && xi >= 320.0 && xi < 3520.0) {
        float n = lerp(0.02, 400.0, (xi - 320.0) / 3200.0);
        c = pq3(float3(n, n, n));
    }

    // cyan / lavender chips
    if (xi >= 120.0 && xi < 420.0 && yi >= 160.0 && yi < 360.0) c = pq3(float3(0.0, 160.0, 140.0));
    if (xi >= u_res_t.x - 420.0 && xi < u_res_t.x - 120.0 && yi >= 160.0 && yi < 360.0)
        c = pq3(float3(130.0, 70.0, 200.0));

    // text masks (coverage x colour in PQ space)
    float wa = mask(tWordmark, u_wordmark, px);
    c = lerp(c, pq3(u_wordmark_nits.xyz), wa);
    float la = mask(tLabel, u_label, px);
    c = lerp(c, pq3(u_label_nits.xyz), la);
    float ca = mask(tClock, u_clock, px);
    c = lerp(c, pq3(u_clock_nits.xyz), ca);
    float ha = mask(tHud, u_hud, px);
    c = lerp(c, pq3(u_hud_nits.xyz), ha);

    // Spark: travelling highlight along the bottom line
    float phase = frac(u_res_t.z * 0.18);
    float sx = u_res_t.x * (0.22 + 0.56 * phase);
    float sy = 1880.0;
    float d = distance(px, float2(sx, sy));
    float spark = pow(saturate(1.0 - d / 70.0), 2.0);
    c += spark * pq3(u_spark_nits.xyz);

    // moving HDR highlight blob
    float hx = u_res_t.x * (0.55 + 0.18 * sin(u_res_t.z * 0.7));
    float hy = u_res_t.y * (0.32 + 0.06 * cos(u_res_t.z * 0.9));
    float d2 = distance(px, float2(hx, hy));
    float blob = pow(saturate(1.0 - d2 / 80.0), 2.0);
    c += blob * pq3(u_blob_nits.xyz);

    c = clamp(c, 0.0, 1.0);
    return float4(c, 1.0);
}
"#;

// ── orientation quirk ───────────────────────────────────────────────────
//
// P0 freeze: the Amlogic 4K HEVC video plane on the Homatics box vertically
// flips THIS encoder's stream (proven against ffmpeg hevc_qsv, which does not
// flip). The correction is a VERTICAL pre-flip of the source, NOT a generic
// 180° rotation — the two differ by a horizontal mirror and are named
// distinctly below.
//
// The quirk is scoped to the live box target, not to the renderer as a
// universal truth: the native scene stays logically upright, and local file
// output (`--out`) is never pre-flipped unless explicitly requested.
//
// Resolution precedence (see `resolve_flip_mode`):
//   1. explicit --flip CLI arg  (none | vertical | rotate180)
//   2. explicit TOASTOVAC_FLIP env override  (0=none, 1=vertical, 2=rotate180)
//   3. device/output capability default:
//        pipe mode  (Homatics live video plane) -> VERTICAL_FLIP
//        file mode  (local diagnostic encode)   -> NONE

#[derive(Clone, Copy, PartialEq, Eq, Debug)]
pub enum FlipMode {
    None,
    Vertical,
    Rotate180,
}

impl FlipMode {
    pub fn as_f32(&self) -> f32 {
        match self {
            FlipMode::None => 0.0,
            FlipMode::Vertical => 1.0,
            FlipMode::Rotate180 => 2.0,
        }
    }

    pub fn name(&self) -> &'static str {
        match self {
            FlipMode::None => "NONE",
            FlipMode::Vertical => "VERTICAL_FLIP",
            FlipMode::Rotate180 => "ROTATE_180",
        }
    }
}

/// Resolve the effective flip mode with the documented precedence:
/// explicit --flip CLI arg, then TOASTOVAC_FLIP env override, then the
/// display-profile default (vertical for the live box path, none for local
/// file encodes). `cli` is the raw `--flip` value ("auto" = unset).
pub fn resolve_flip_mode(cli: &str, file_mode: bool) -> FlipMode {
    match cli {
        "none" => return FlipMode::None,
        "vertical" => return FlipMode::Vertical,
        "rotate180" => return FlipMode::Rotate180,
        _ => {}
    }
    if let Ok(v) = std::env::var("TOASTOVAC_FLIP") {
        match v.as_str() {
            "0" => return FlipMode::None,
            "1" => return FlipMode::Vertical,
            "2" => return FlipMode::Rotate180,
            _ => {} // invalid value -> fall through to the profile default
        }
    }
    if file_mode {
        // Local diagnostic encodes stay logically upright (the Amlogic
        // video-plane quirk does not apply to a file on the PC).
        FlipMode::None
    } else {
        // displayProfile target=HOMATICS_AMLOGIC_VIDEO_PLANE verticalPreFlip=true
        FlipMode::Vertical
    }
}

// ── cbuffer ─────────────────────────────────────────────────────────────

#[repr(C)]
#[derive(Clone, Copy)]
pub struct SceneCB {
    pub u_res_t: [f32; 4],
    pub u_clock: [f32; 4],
    pub u_hud: [f32; 4],
    pub u_wordmark: [f32; 4],
    pub u_label: [f32; 4],
    pub u_bg_nits: [f32; 4],
    pub u_wordmark_nits: [f32; 4],
    pub u_label_nits: [f32; 4],
    pub u_clock_nits: [f32; 4],
    pub u_hud_nits: [f32; 4],
    pub u_spark_nits: [f32; 4],
    pub u_blob_nits: [f32; 4],
    pub u_hairline_nits: [f32; 4],
    pub u_flip: [f32; 4],
}

impl SceneCB {
    pub fn new(flip: FlipMode) -> Self {
        SceneCB {
            u_res_t: [W as f32, H as f32, 0.0, 0.0],
            u_clock: [0.0; 4],
            u_hud: [0.0; 4],
            u_wordmark: [0.0; 4],
            u_label: [0.0; 4],
            u_bg_nits: [0.04, 0.04, 0.05, 0.0],
            u_wordmark_nits: [180.0, 180.0, 200.0, 0.0],
            u_label_nits: [0.0, 140.0, 130.0, 0.0],
            u_clock_nits: [160.0, 160.0, 175.0, 0.0],
            u_hud_nits: [90.0, 90.0, 110.0, 0.0],
            u_spark_nits: [700.0, 160.0, 860.0, 0.0],
            u_blob_nits: [900.0, 900.0, 900.0, 0.0],
            u_hairline_nits: [80.0, 80.0, 80.0, 0.0],
            u_flip: [flip.as_f32(), 0.0, 0.0, 0.0],
        }
    }
}

// ── text raster (D2D + DWrite) ──────────────────────────────────────────

pub struct TextRaster {
    _factory: ID2D1Factory1,
    _d2d_device: ID2D1Device,
    pub dc: ID2D1DeviceContext,
    rt: ID2D1RenderTarget,
    _dwrite: IDWriteFactory,
    white: ID2D1SolidColorBrush,
}

fn wide(s: &str) -> Vec<u16> {
    s.encode_utf16().collect()
}

impl TextRaster {
    pub fn new(d3d_device: &ID3D11Device) -> Result<Self, String> {
        unsafe {
            let factory: ID2D1Factory1 = D2D1CreateFactory(D2D1_FACTORY_TYPE_SINGLE_THREADED, None)
                .map_err(|e| format!("D2D1CreateFactory: {e}"))?;
            let dxgi_device: IDXGIDevice = d3d_device
                .cast()
                .map_err(|e| format!("cast IDXGIDevice: {e}"))?;
            let d2d_device: ID2D1Device = factory
                .CreateDevice(&dxgi_device)
                .map_err(|e| format!("CreateDevice: {e}"))?;
            let dc: ID2D1DeviceContext = d2d_device
                .CreateDeviceContext(D2D1_DEVICE_CONTEXT_OPTIONS_NONE)
                .map_err(|e| format!("CreateDeviceContext: {e}"))?;
            let dwrite: IDWriteFactory = DWriteCreateFactory(DWRITE_FACTORY_TYPE_SHARED)
                .map_err(|e| format!("DWrite: {e}"))?;
            let rt: ID2D1RenderTarget = dc.cast().map_err(|e| format!("cast RenderTarget: {e}"))?;
            let color = D2D1_COLOR_F { r: 1.0, g: 1.0, b: 1.0, a: 1.0 };
            let white = rt
                .CreateSolidColorBrush(&color, None)
                .map_err(|e| format!("brush: {e}"))?;
            Ok(TextRaster {
                _factory: factory,
                _d2d_device: d2d_device,
                dc,
                rt,
                _dwrite: dwrite,
                white,
            })
        }
    }

    /// Rasterize `text` (family "Arial", size px) into a BGRA8 texture.
    /// Returns (texture, width_px, height_px). White coverage in .r.
    pub fn raster(
        &self,
        device: &ID3D11Device,
        text: &str,
        font_size: f32,
    ) -> Result<(ID3D11Texture2D, u32, u32), String> {
        unsafe {
            let family = wide("Arial\0");
            let locale = wide("en-us\0");
            let fmt: IDWriteTextFormat = self
                ._dwrite
                .CreateTextFormat(
                    PCWSTR(family.as_ptr()),
                    None,
                    DWRITE_FONT_WEIGHT_NORMAL,
                    DWRITE_FONT_STYLE_NORMAL,
                    DWRITE_FONT_STRETCH_NORMAL,
                    font_size,
                    PCWSTR(locale.as_ptr()),
                )
                .map_err(|e| format!("CreateTextFormat: {e}"))?;
            let wide = wide(text);
            let layout = self
                ._dwrite
                .CreateTextLayout(&wide, &fmt, 8192.0, 1024.0)
                .map_err(|e| format!("CreateTextLayout: {e}"))?;
            let mut m = DWRITE_TEXT_METRICS::default();
            layout.GetMetrics(&mut m).map_err(|e| format!("GetMetrics: {e}"))?;
            let tw = (m.width.ceil() as u32).max(8) + 8;
            let th = (m.height.ceil() as u32).max(8) + 8;

            let desc = D3D11_TEXTURE2D_DESC {
                Width: tw,
                Height: th,
                MipLevels: 1,
                ArraySize: 1,
                Format: DXGI_FORMAT_B8G8R8A8_UNORM,
                SampleDesc: DXGI_SAMPLE_DESC { Count: 1, Quality: 0 },
                Usage: D3D11_USAGE_DEFAULT,
                BindFlags: (D3D11_BIND_RENDER_TARGET.0 | D3D11_BIND_SHADER_RESOURCE.0) as u32,
                CPUAccessFlags: 0,
                MiscFlags: 0,
            };
            let mut tex: Option<ID3D11Texture2D> = None;
            device
                .CreateTexture2D(&desc, None, Some(&mut tex))
                .map_err(|e| format!("CreateTexture2D: {e}"))?;
            let tex = tex.ok_or("tex null")?;
            let surface: IDXGISurface = tex.cast().map_err(|e| format!("cast IDXGISurface: {e}"))?;
            let props = D2D1_BITMAP_PROPERTIES1 {
                pixelFormat: D2D1_PIXEL_FORMAT {
                    format: DXGI_FORMAT_B8G8R8A8_UNORM,
                    alphaMode: D2D1_ALPHA_MODE_PREMULTIPLIED,
                },
                dpiX: 96.0,
                dpiY: 96.0,
                bitmapOptions: D2D1_BITMAP_OPTIONS_TARGET,
                colorContext: std::mem::ManuallyDrop::new(None),
            };
            let bitmap: ID2D1Bitmap1 = self
                .dc
                .CreateBitmapFromDxgiSurface(&surface, Some(&props))
                .map_err(|e| format!("CreateBitmapFromDxgiSurface: {e}"))?;
            let img: ID2D1Image = bitmap.cast().map_err(|e| format!("cast ID2D1Image: {e}"))?;
            self.dc.SetTarget(Some(&img));
            let _ = self.rt.BeginDraw();
            let clear = D2D1_COLOR_F { r: 0.0, g: 0.0, b: 0.0, a: 0.0 };
            let _ = self.rt.Clear(Some(&clear));
            let rect = D2D_RECT_F {
                left: 4.0,
                top: 2.0,
                right: tw as f32 - 4.0,
                bottom: th as f32 - 2.0,
            };
            self.rt.DrawText(
                &wide,
                &fmt,
                &rect,
                &self.white,
                D2D1_DRAW_TEXT_OPTIONS_NONE,
                DWRITE_MEASURING_MODE_NATURAL,
            );
            let _ = self.rt.EndDraw(None, None);
            self.dc.SetTarget(None);
            Ok((tex, tw, th))
        }
    }
}

// ── scene renderer ──────────────────────────────────────────────────────

pub struct SceneRenderer {
    pub device: ID3D11Device,
    pub context: ID3D11DeviceContext,
    pub rt: ID3D11Texture2D,
    pub rt_rtv: ID3D11RenderTargetView,
    pub rt_srv: ID3D11ShaderResourceView,
    ps: ID3D11PixelShader,
    vs: ID3D11VertexShader,
    rs: ID3D11RasterizerState,
    cb: ID3D11Buffer,
    cb_data: SceneCB,
    wordmark: Option<(ID3D11Texture2D, u32, u32)>,
    label: Option<(ID3D11Texture2D, u32, u32)>,
    clock: Option<(ID3D11Texture2D, u32, u32)>,
    hud: Option<(ID3D11Texture2D, u32, u32)>,
    clock_key: String,
    hud_key: String,
    pub text: TextRaster,
}

impl SceneRenderer {
    pub fn new(
        device: &ID3D11Device,
        context: &ID3D11DeviceContext,
        flip: FlipMode,
    ) -> Result<Self, String> {
        unsafe {
            let vs_blob = compile(VS, "main", "vs_5_0")?;
            let ps_blob = compile(PS, "main", "ps_5_0")?;
            let vs_bytes = blob_bytes(&vs_blob).to_vec();
            let ps_bytes = blob_bytes(&ps_blob).to_vec();
            let mut vs: Option<ID3D11VertexShader> = None;
            device
                .CreateVertexShader(&vs_bytes, None, Some(&mut vs))
                .map_err(|e| format!("CreateVertexShader: {e}"))?;
            let mut ps: Option<ID3D11PixelShader> = None;
            device
                .CreatePixelShader(&ps_bytes, None, Some(&mut ps))
                .map_err(|e| format!("CreatePixelShader: {e}"))?;

            // Fullscreen triangle via SV_VertexID is counter-clockwise in NDC;
            // the default rasterizer state (CullMode=Back) culls it, leaving the
            // RT black. CullMode=None makes the triangle survive regardless of
            // winding.
            let rs_desc = D3D11_RASTERIZER_DESC {
                FillMode: D3D11_FILL_SOLID,
                CullMode: D3D11_CULL_NONE,
                FrontCounterClockwise: false.into(),
                DepthBias: 0,
                DepthBiasClamp: 0.0,
                SlopeScaledDepthBias: 0.0,
                DepthClipEnable: false.into(),
                ScissorEnable: false.into(),
                MultisampleEnable: false.into(),
                AntialiasedLineEnable: false.into(),
            };
            let mut rs: Option<ID3D11RasterizerState> = None;
            device
                .CreateRasterizerState(&rs_desc, Some(&mut rs))
                .map_err(|e| format!("CreateRasterizerState: {e}"))?;

            let rt_desc = D3D11_TEXTURE2D_DESC {
                Width: W,
                Height: H,
                MipLevels: 1,
                ArraySize: 1,
                Format: DXGI_FORMAT_R16G16B16A16_FLOAT,
                SampleDesc: DXGI_SAMPLE_DESC { Count: 1, Quality: 0 },
                Usage: D3D11_USAGE_DEFAULT,
                BindFlags: (D3D11_BIND_RENDER_TARGET.0 | D3D11_BIND_SHADER_RESOURCE.0) as u32,
                CPUAccessFlags: 0,
                MiscFlags: 0,
            };
            let mut rt: Option<ID3D11Texture2D> = None;
            device
                .CreateTexture2D(&rt_desc, None, Some(&mut rt))
                .map_err(|e| format!("RT texture: {e}"))?;
            let rt = rt.ok_or("rt null")?;
            let rtv_desc = D3D11_RENDER_TARGET_VIEW_DESC {
                Format: DXGI_FORMAT_R16G16B16A16_FLOAT,
                ViewDimension: windows::Win32::Graphics::Direct3D11::D3D11_RTV_DIMENSION_TEXTURE2D,
                Anonymous: windows::Win32::Graphics::Direct3D11::D3D11_RENDER_TARGET_VIEW_DESC_0 {
                    Texture2D: D3D11_TEX2D_RTV { MipSlice: 0 },
                },
            };
            let mut rt_rtv: Option<ID3D11RenderTargetView> = None;
            device
                .CreateRenderTargetView(&rt, Some(&rtv_desc), Some(&mut rt_rtv))
                .map_err(|e| format!("RTV: {e}"))?;
            let srv_desc = D3D11_SHADER_RESOURCE_VIEW_DESC {
                Format: DXGI_FORMAT_R16G16B16A16_FLOAT,
                ViewDimension: D3D11_SRV_DIMENSION_TEXTURE2D,
                Anonymous: windows::Win32::Graphics::Direct3D11::D3D11_SHADER_RESOURCE_VIEW_DESC_0 {
                    Texture2D: D3D11_TEX2D_SRV {
                        MostDetailedMip: 0,
                        MipLevels: 1,
                    },
                },
            };
            let mut rt_srv: Option<ID3D11ShaderResourceView> = None;
            device
                .CreateShaderResourceView(&rt, Some(&srv_desc), Some(&mut rt_srv))
                .map_err(|e| format!("SRV: {e}"))?;

            let cb_desc = D3D11_BUFFER_DESC {
                ByteWidth: std::mem::size_of::<SceneCB>() as u32,
                Usage: D3D11_USAGE_DYNAMIC,
                BindFlags: D3D11_BIND_CONSTANT_BUFFER.0 as u32,
                CPUAccessFlags: D3D11_CPU_ACCESS_WRITE.0 as u32,
                MiscFlags: 0,
                StructureByteStride: 0,
            };
            let mut cb: Option<ID3D11Buffer> = None;
            device
                .CreateBuffer(&cb_desc, None, Some(&mut cb))
                .map_err(|e| format!("cbuffer: {e}"))?;

            let text = TextRaster::new(device)?;
            let mut r = SceneRenderer {
                device: device.clone(),
                context: context.clone(),
                rt,
                rt_rtv: rt_rtv.ok_or("rtv null")?,
                rt_srv: rt_srv.ok_or("srv null")?,
                ps: ps.ok_or("ps null")?,
                vs: vs.ok_or("vs null")?,
                rs: rs.ok_or("rs null")?,
                cb: cb.ok_or("cb null")?,
                cb_data: SceneCB::new(flip),
                wordmark: None,
                label: None,
                clock: None,
                hud: None,
                clock_key: String::new(),
                hud_key: String::new(),
                text,
            };
            r.build_static_masks()?;
            Ok(r)
        }
    }

    fn build_static_masks(&mut self) -> Result<(), String> {
        let (wt, ww, wh) = self.text.raster(&self.device, "Toastovač", 176.0)?;
        let (lt, lw, lh) = self.text.raster(&self.device, "live 4K60  PQ  BT.2020  Main10", 36.0)?;
        let xw = ((W as i64 - ww as i64) / 2).max(0) as f32;
        self.wordmark = Some((wt, ww, wh));
        self.cb_data.u_wordmark = [xw, 860.0, ww as f32, wh as f32];
        let xl = ((W as i64 - lw as i64) / 2).max(0) as f32;
        self.label = Some((lt, lw, lh));
        self.cb_data.u_label = [xl, 1180.0, lw as f32, lh as f32];
        Ok(())
    }

    pub fn update_clock(&mut self, clock: &str) -> Result<(), String> {
        if clock == self.clock_key && self.clock.is_some() {
            return Ok(());
        }
        let (t, tw, th) = self.text.raster(&self.device, clock, 72.0)?;
        let x = ((W as i64 - tw as i64) / 2).max(0) as f32;
        self.cb_data.u_clock = [x, 1080.0, tw as f32, th as f32];
        self.clock = Some((t, tw, th));
        self.clock_key = clock.to_string();
        Ok(())
    }

    pub fn update_hud(&mut self, line: &str) -> Result<(), String> {
        if line == self.hud_key && self.hud.is_some() {
            return Ok(());
        }
        let (t, tw, th) = self.text.raster(&self.device, line, 28.0)?;
        self.cb_data.u_hud = [40.0, H as f32 - 70.0 - th as f32, tw as f32, th as f32];
        self.hud = Some((t, tw, th));
        self.hud_key = line.to_string();
        Ok(())
    }

    /// Render one frame into the RGBA16F RT. No CPU readback happens here.
    pub fn render(&mut self, t: f32, clock: &str, hud: &str) -> Result<(), String> {
        unsafe {
            self.update_clock(clock)?;
            self.update_hud(hud)?;
            self.cb_data.u_res_t[2] = t;

            let mut mapped = windows::Win32::Graphics::Direct3D11::D3D11_MAPPED_SUBRESOURCE::default();
            self.context
                .Map(&self.cb, 0, D3D11_MAP_WRITE_DISCARD, 0, Some(&mut mapped))
                .map_err(|e| format!("Map cbuffer: {e}"))?;
            std::ptr::copy_nonoverlapping(
                &self.cb_data as *const SceneCB as *const u8,
                mapped.pData as *mut u8,
                std::mem::size_of::<SceneCB>(),
            );
            self.context.Unmap(&self.cb, 0);

            let vp = D3D11_VIEWPORT {
                TopLeftX: 0.0,
                TopLeftY: 0.0,
                Width: W as f32,
                Height: H as f32,
                MinDepth: 0.0,
                MaxDepth: 1.0,
            };
            self.context.RSSetViewports(Some(&[vp]));
            self.context
                .OMSetRenderTargets(Some(&[Some(self.rt_rtv.clone())]), None);
            self.context.RSSetState(Some(&self.rs));
            self.context
                .ClearRenderTargetView(&self.rt_rtv, &[0.0, 0.0, 0.0, 1.0]);
            self.context.VSSetShader(Some(&self.vs), None);
            self.context.PSSetShader(Some(&self.ps), None);
            self.context.IASetInputLayout(None);
            self.context
                .IASetPrimitiveTopology(D3D_PRIMITIVE_TOPOLOGY_TRIANGLELIST);
            self.context.IASetVertexBuffers(0, 0, None, None, None);
            self.context.VSSetConstantBuffers(0, Some(&[Some(self.cb.clone())]));
            self.context.PSSetConstantBuffers(0, Some(&[Some(self.cb.clone())]));
            let mut srvs: Vec<Option<ID3D11ShaderResourceView>> = Vec::new();
            for m in [&self.wordmark, &self.label, &self.clock, &self.hud] {
                if let Some((tex, _, _)) = m {
                    let d = D3D11_SHADER_RESOURCE_VIEW_DESC {
                        Format: DXGI_FORMAT_B8G8R8A8_UNORM,
                        ViewDimension: D3D11_SRV_DIMENSION_TEXTURE2D,
                        Anonymous: windows::Win32::Graphics::Direct3D11::D3D11_SHADER_RESOURCE_VIEW_DESC_0 {
                            Texture2D: D3D11_TEX2D_SRV {
                                MostDetailedMip: 0,
                                MipLevels: 1,
                            },
                        },
                    };
                    let mut srv: Option<ID3D11ShaderResourceView> = None;
                    self.device
                        .CreateShaderResourceView(tex, Some(&d), Some(&mut srv))
                        .map_err(|e| format!("mask SRV: {e}"))?;
                    srvs.push(Some(srv.ok_or("srv null")?));
                } else {
                    srvs.push(None);
                }
            }
            self.context.PSSetShaderResources(0, Some(&srvs));
            self.context.Draw(3, 0);
            // Unbind the RT from the OM stage: the compute convert reads this
            // texture as an SRV on the same immediate context, and a resource
            // bound as RTV+SRV simultaneously is undefined behavior (often
            // renders black on Intel).
            self.context.OMSetRenderTargets(None, None);
            self.context.RSSetState(None);
            Ok(())
        }
    }

    pub fn fmt_render_target(&self) -> &'static str {
        format_name(DXGI_FORMAT_R16G16B16A16_FLOAT)
    }
}