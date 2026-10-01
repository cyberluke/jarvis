//! D3D11 device + Intel adapter selection + shader compilation helpers.

#![allow(dead_code)]

use std::ffi::c_void;
use std::ptr::null_mut;

use windows::core::Interface as _;
use windows::core::PCSTR;
use windows::Win32::Foundation::HMODULE;
use windows::Win32::Graphics::Direct3D::{
    D3D_DRIVER_TYPE_UNKNOWN, D3D_FEATURE_LEVEL, D3D_FEATURE_LEVEL_11_0, D3D_FEATURE_LEVEL_11_1,
    D3D_FEATURE_LEVEL_12_0, D3D_FEATURE_LEVEL_12_1, ID3DBlob,
};
use windows::Win32::Graphics::Direct3D::Fxc::D3DCompile;
use windows::Win32::Graphics::Direct3D10::ID3D10Multithread;
use windows::Win32::Graphics::Direct3D11::{
    D3D11CreateDevice, D3D11_CREATE_DEVICE_BGRA_SUPPORT, D3D11_CREATE_DEVICE_VIDEO_SUPPORT,
    D3D11_SDK_VERSION, ID3D11Device, ID3D11DeviceContext,
};
use windows::Win32::Graphics::Dxgi::{
    CreateDXGIFactory1, DXGI_ADAPTER_DESC1, IDXGIAdapter, IDXGIAdapter1, IDXGIFactory1,
};
use windows::Win32::Graphics::Dxgi::Common::{DXGI_FORMAT, 
    DXGI_FORMAT_B8G8R8A8_UNORM, DXGI_FORMAT_P010, DXGI_FORMAT_R16_UINT, DXGI_FORMAT_R16G16_UINT,
    DXGI_FORMAT_R16G16B16A16_FLOAT,
};

pub const VENDOR_INTEL: u32 = 0x8086;
pub const VENDOR_NVIDIA: u32 = 0x10DE;

pub struct AdapterInfo {
    pub name: String,
    pub vendor: u32,
    pub device: u32,
    pub luid_low: u32,
    pub luid_high: i32,
    pub description: String,
}

pub struct Device {
    pub device: ID3D11Device,
    pub context: ID3D11DeviceContext,
    pub feature_level: u32,
    pub adapter: AdapterInfo,
}

pub fn create_dxgi_factory() -> Result<IDXGIFactory1, String> {
    unsafe { CreateDXGIFactory1().map_err(|e| format!("CreateDXGIFactory1: {e}")) }
}

/// Enumerate adapters and pick the Intel one. Hard-reject NVIDIA.
pub fn pick_intel_adapter(factory: &IDXGIFactory1) -> Result<AdapterInfo, String> {
    unsafe {
        let mut i: u32 = 0;
        loop {
            match factory.EnumAdapters1(i) {
                Ok(a) => {
                    let d: DXGI_ADAPTER_DESC1 = a
                        .GetDesc1()
                        .map_err(|e| format!("GetDesc1: {e}"))?;
                    let name = String::from_utf16_lossy(&d.Description);
                    if d.VendorId == VENDOR_INTEL {
                        return Ok(AdapterInfo {
                            name: name.trim_end_matches('\0').to_string(),
                            vendor: d.VendorId,
                            device: d.DeviceId,
                            luid_low: d.AdapterLuid.LowPart,
                            luid_high: d.AdapterLuid.HighPart,
                            description: format!(
                                "Intel adapter {} (ven={:04x} dev={:04x} luid={:08x}{:08x})",
                                name.trim_end_matches('\0'),
                                d.VendorId,
                                d.DeviceId,
                                d.AdapterLuid.HighPart,
                                d.AdapterLuid.LowPart
                            ),
                        });
                    }
                    i += 1;
                }
                Err(_) => break,
            }
        }
        Err("no Intel adapter found (RTX/NVIDIA adapters are hard-rejected)".into())
    }
}

/// Find the IDXGIAdapter1 matching a previously discovered Intel adapter.
pub fn find_adapter(
    factory: &IDXGIFactory1,
    vendor: u32,
    device: u32,
) -> Result<IDXGIAdapter1, String> {
    unsafe {
        let mut i: u32 = 0;
        loop {
            match factory.EnumAdapters1(i) {
                Ok(a) => {
                    let d = a.GetDesc1().map_err(|e| format!("GetDesc1: {e}"))?;
                    if d.VendorId == vendor && d.DeviceId == device {
                        return Ok(a);
                    }
                    i += 1;
                }
                Err(_) => break,
            }
        }
        Err("matching Intel adapter not found".into())
    }
}

/// Create a D3D11 device on the given Intel adapter with BGRA support
/// (required for the D2D text raster path).
pub fn create_device_on(adapter: &IDXGIAdapter1) -> Result<Device, String> {
    unsafe {
        let mut dev: Option<ID3D11Device> = None;
        let mut ctx: Option<ID3D11DeviceContext> = None;
        let mut fl: D3D_FEATURE_LEVEL = D3D_FEATURE_LEVEL_11_0;
        let adapter_base: IDXGIAdapter = adapter
            .cast()
            .map_err(|e| format!("adapter cast: {e}"))?;
        let levels = [
            D3D_FEATURE_LEVEL_12_1,
            D3D_FEATURE_LEVEL_12_0,
            D3D_FEATURE_LEVEL_11_1,
            D3D_FEATURE_LEVEL_11_0,
        ];
        // FFmpeg's d3d11va device uses VIDEO_SUPPORT only. BGRA is needed for
        // our D2D text raster; env TOASTOVAC_NO_BGRA probes runtime sensitivity.
        let no_bgra = std::env::var("TOASTOVAC_NO_BGRA").map(|v| v == "1").unwrap_or(false);
        let flags = if no_bgra {
            D3D11_CREATE_DEVICE_VIDEO_SUPPORT
        } else {
            D3D11_CREATE_DEVICE_BGRA_SUPPORT | D3D11_CREATE_DEVICE_VIDEO_SUPPORT
        };
        eprintln!("D3D11 flags=0x{:x}", flags.0);
        D3D11CreateDevice(
            Some(&adapter_base),
            D3D_DRIVER_TYPE_UNKNOWN,
            HMODULE::default(),
            flags,
            Some(&levels),
            D3D11_SDK_VERSION,
            Some(&mut dev),
            Some(&mut fl),
            Some(&mut ctx),
        )
        .map_err(|e| format!("D3D11CreateDevice on Intel adapter: {e}"))?;
        let dev = dev.ok_or("device null")?;
        let ctx = ctx.ok_or("context null")?;
        // The iHD/mfx-gen runtime requires multithread protection on the
        // D3D11 device (FFmpeg does the same in hwcontext_d3d11va.c).
        if let Ok(mt) = dev.cast::<ID3D10Multithread>() {
            let _ = mt.SetMultithreadProtected(true);
        }
        let desc = adapter.GetDesc1().map_err(|e| format!("GetDesc1: {e}"))?;
        Ok(Device {
            device: dev,
            context: ctx,
            feature_level: fl.0 as u32,
            adapter: AdapterInfo {
                name: String::from_utf16_lossy(&desc.Description)
                    .trim_end_matches('\0')
                    .to_string(),
                vendor: desc.VendorId,
                device: desc.DeviceId,
                luid_low: desc.AdapterLuid.LowPart,
                luid_high: desc.AdapterLuid.HighPart,
                description: String::new(),
            },
        })
    }
}

/// Compile an HLSL shader at runtime via d3dcompiler_47.
pub fn compile(src: &str, entry: &str, target: &str) -> Result<ID3DBlob, String> {
    unsafe {
        let entry_z = format!("{entry}\0");
        let target_z = format!("{target}\0");
        let mut code: Option<ID3DBlob> = None;
        let mut err: Option<ID3DBlob> = None;
        let hr = D3DCompile(
            src.as_ptr() as *const c_void,
            src.len(),
            None,
            None,
            None,
            PCSTR::from_raw(entry_z.as_ptr()),
            PCSTR::from_raw(target_z.as_ptr()),
            0,
            0,
            &mut code,
            Some(&mut err),
        );
        if hr.is_ok() {
            if let Some(c) = code {
                return Ok(c);
            }
        }
        let msg = if let Some(e) = err {
            let p = e.GetBufferPointer() as *const u8;
            let n = e.GetBufferSize();
            String::from_utf8_lossy(std::slice::from_raw_parts(p, n)).to_string()
        } else {
            format!("D3DCompile failed hr={hr:?}")
        };
        Err(format!("HLSL compile {entry}@{target} failed: {}", msg.trim()))
    }
}

pub fn blob_bytes(blob: &ID3DBlob) -> &[u8] {
    unsafe {
        std::slice::from_raw_parts(
            blob.GetBufferPointer() as *const u8,
            blob.GetBufferSize(),
        )
    }
}

pub fn format_name(f: DXGI_FORMAT) -> &'static str {
    if f == DXGI_FORMAT_R16G16B16A16_FLOAT {
        "RGBA16F"
    } else if f == DXGI_FORMAT_P010 {
        "P010"
    } else if f == DXGI_FORMAT_R16_UINT {
        "R16_UINT"
    } else if f == DXGI_FORMAT_R16G16_UINT {
        "R16G16_UINT"
    } else if f == DXGI_FORMAT_B8G8R8A8_UNORM {
        "BGRA8"
    } else {
        "OTHER"
    }
}