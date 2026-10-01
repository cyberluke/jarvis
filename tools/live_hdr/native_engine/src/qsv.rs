//! oneVPL QSV encoder: hardware session on the Intel D3D11 device, custom
//! P010 frame allocator (DECODER|SRV|UAV bindings), zero-copy encode of
//! GPU-resident frames, Annex-B AU output.

#![allow(dead_code)]

use std::ffi::c_void;
use std::mem::size_of;
use std::ptr::null_mut;

use windows::core::Interface as _;
use windows::Win32::Graphics::Direct3D11::{ID3D11Device, ID3D11Texture2D};

use crate::convert::Converter;
use crate::mfx::*;

// Must be >= BufferSizeInKB*1000 (hevcehw_base_legacy.cpp BLK_CheckBsData:
// MaxLength must hold the whole VBV buffer; 5000 KB -> 5,000,000 bytes).
const BITSTREAM_CAP: u32 = 16 * 1024 * 1024;

/// Diagnostic: trace allocator callbacks from the runtime (TOASTOVAC_TRACE_ALLOC=1).
fn alloc_trace() -> bool {
    std::env::var("TOASTOVAC_TRACE_ALLOC").map(|v| v == "1").unwrap_or(false)
}

/// Allocator state handed to the oneVPL callbacks via pthis.
struct AllocCtx {
    device: ID3D11Device,
    /// Every texture this allocator created (input pool + internal).
    textures: Vec<ID3D11Texture2D>,
    /// Persistent mfxHDLPair boxes; MemIds point into these (GetHDL returns
    /// the pair pointer directly, per the oneVPL D3D11 allocator contract).
    pairs: Vec<Box<mfxHDLPair>>,
    /// Pre-made pool MemIds (created before Init) shared with the encoder.
    pool: Vec<mfxMemId>,
    /// Persistent mids arrays, one per Alloc call.
    records: Vec<Vec<mfxMemId>>,
}

unsafe extern "C" fn alloc_cb(
    pthis: mfxHDL,
    request: *mut mfxFrameAllocRequest,
    response: *mut mfxFrameAllocResponse,
) -> i32 {
    let ctx = &mut *(pthis as *mut AllocCtx);
    if request.is_null() || response.is_null() {
        return MFX_ERR_NULL_PTR;
    }
    let req = &*request;
    let info = &req.Info;
    let count = req.NumFrameSuggested.max(1) as usize;
    if alloc_trace() {
        eprintln!(
            "ALLOC req type=0x{:x} fourcc=0x{:x} w={} h={} crop={}x{} numMin={} numSuggested={} allocId=0x{:x}",
            req.Type, info.FourCC, info.Width, info.Height, info.CropW, info.CropH,
            req.NumFrameMin, req.NumFrameSuggested, req.AllocId
        );
    }
    let mut mids: Vec<mfxMemId> = Vec::with_capacity(count);
    // Prefer the pre-made pool (built before Init) so the encoder's pool
    // matches the surfaces the app submits; fall back to creating textures
    // on demand, each backed by a persistent mfxHDLPair as its MemId.
    if !ctx.pool.is_empty() {
        for i in 0..count {
            mids.push(ctx.pool[i % ctx.pool.len()]);
        }
    } else {
        for _ in 0..count {
            match Converter::create_p010_surface(&ctx.device, info.Width as u32, info.Height as u32) {
                Ok(tex) => {
                    let ptr = windows::core::Interface::as_raw(&tex) as *mut c_void;
                    let pair = Box::new(mfxHDLPair {
                        first: ptr,
                        second: 0 as mfxHDL,
                    });
                    ctx.textures.push(tex);
                    ctx.pairs.push(pair);
                    mids.push(ptr); // MemId = raw texture pointer
                }
                Err(e) => {
                    eprintln!("ALLOC_FAIL {e}");
                    return MFX_ERR_MEMORY_ALLOC;
                }
            }
        }
    }
    ctx.records.push(mids.clone());
    let resp = &mut *response;
    resp.mids = ctx.records.last_mut().unwrap().as_mut_ptr();
    resp.NumFrameActual = count as u16;
    resp.AllocId = req.AllocId;
    if alloc_trace() {
        eprintln!("ALLOC -> numActual={} firstMid={:p}", resp.NumFrameActual, resp.mids);
    }
    MFX_ERR_NONE
}

unsafe extern "C" fn lock_cb(_pthis: mfxHDL, mid: mfxMemId, _ptr: *mut mfxFrameData) -> i32 {
    if alloc_trace() {
        eprintln!("LOCK mid={:p}", mid);
    }
    // D3D11 video memory: the encoder uses GetHDL, not Lock.
    MFX_ERR_UNSUPPORTED
}

unsafe extern "C" fn unlock_cb(_pthis: mfxHDL, mid: mfxMemId, _ptr: *mut mfxFrameData) -> i32 {
    if alloc_trace() {
        eprintln!("UNLOCK mid={:p}", mid);
    }
    MFX_ERR_UNSUPPORTED
}

unsafe extern "C" fn gethdl_cb(
    _pthis: mfxHDL,
    mid: mfxMemId,
    handle: *mut mfxHDL,
) -> i32 {
    if handle.is_null() {
        return MFX_ERR_NULL_PTR;
    }
    if alloc_trace() {
        eprintln!("GETHDL mid={:p}", mid);
    }
    // FFmpeg frame_get_hdl convention (hwcontext_qsv.c): the runtime passes a
    // buffer for an mfxHDLPair; fill it with { first: ID3D11Texture2D*,
    // second: array index }. mid IS the raw texture pointer (external
    // app-provided surface). Without this the encode task cannot resolve the
    // input texture and never produces an AU.
    let pair = handle as *mut mfxHDLPair;
    (*pair).first = mid;
    (*pair).second = 0 as mfxHDL;
    MFX_ERR_NONE
}

unsafe extern "C" fn free_cb(
    pthis: mfxHDL,
    response: *mut mfxFrameAllocResponse,
) -> i32 {
    let ctx = &mut *(pthis as *mut AllocCtx);
    if response.is_null() {
        return MFX_ERR_NULL_PTR;
    }
    let resp = &*response;
    if alloc_trace() {
        eprintln!("FREE numActual={} firstMid={:p}", resp.NumFrameActual, resp.mids);
    }
    // Release every texture whose pointer appears in this response.
    if !resp.mids.is_null() {
        let n = resp.NumFrameActual as usize;
        let slice = std::slice::from_raw_parts(resp.mids, n);
        let ptrs: Vec<usize> = slice.iter().map(|m| *m as usize).collect();
        ctx.textures.retain(|t| {
            let p = windows::core::Interface::as_raw(t) as usize;
            !ptrs.contains(&p)
        });
    }
    MFX_ERR_NONE
}

pub struct EncoderInit {
    pub fps: u32,
    pub fps_den: u32,
    pub bitrate_kbps: u32,
    pub max_kbps: u32,
    pub bufsize_kb: u32,
    pub gop: u32,
}

pub struct Encoder {
    pub session: mfxSession,
    _loader: mfxLoader,
    pub device: ID3D11Device,
    ctx: *mut AllocCtx,
    pub surfaces: Vec<mfxFrameSurface1>,
    pub textures: Vec<ID3D11Texture2D>,
    bs: mfxBitstream,
    bs_storage: Vec<u8>,
    pub actual_level: u16,
    pub api_version: (u16, u16),
    pub impl_name: String,
    fps: u32,     // P5.2: rational rate (num)
    fps_den: u32, // P5.2: rational rate (den)
}

impl Encoder {
    /// Create the oneVPL session (hardware impl via the dispatcher),
    /// register the D3D11 device, and initialise the HEVC Main10 encoder.
    pub fn new(
        device: &ID3D11Device,
        adapter: &crate::d3d::AdapterInfo,
        init: &EncoderInit,
        width: u32,
        height: u32,
    ) -> Result<Self, String> {
        unsafe {
            abi_audit();
            let disp = Dispatcher::load()?;
            let loader = (disp.mfx_load)();
            if loader.is_null() {
                return Err("MFXLoad returned NULL (no oneVPL implementation)".into());
            }
            // enumerate what the dispatcher sees (probe before choosing)
            let mut i: u32 = 0;
            loop {
                let mut idesc: mfxHDL = null_mut();
                let st = (disp.mfx_enum_implementations)(loader, i, 1, &mut idesc);
                if st != MFX_ERR_NONE || idesc.is_null() {
                    break;
                }
                let head = idesc as *const u8;
                let impl_type = *(head.add(4) as *const u32);
                let api_major = *(head.add(12) as *const u16);
                let api_minor = *(head.add(14) as *const u16);
                let name = std::ffi::CStr::from_ptr(head.add(16) as *const i8)
                    .to_string_lossy()
                    .to_string();
                eprintln!("IMPL[{i}] type=0x{impl_type:x} api={api_major}.{api_minor} name={name}");
                let _ = (disp.mfx_disp_release_impl_description)(loader, idesc);
                i += 1;
            }
            eprintln!("IMPL_TOTAL {i}");
            let cfg = (disp.mfx_create_config)(loader);
            if cfg.is_null() {
                return Err("MFXCreateConfig failed".into());
            }
            // Filter: hardware implementation, Intel vendor, D3D11 acceleration
            // (mirrors FFmpeg's libvpl loader setup; without the D3D11
            // acceleration-mode filter the runtime rejects the D3D11 handle).
            let mut st = (disp.mfx_set_config_filter_property)(
                cfg,
                b"mfxImplDescription.Impl\0".as_ptr(),
                variant_u32(MFX_IMPL_TYPE_HARDWARE),
            );
            if st != MFX_ERR_NONE {
                return Err(format!("MFXSetConfigFilterProperty(Impl=HARDWARE) failed: {st}"));
            }
            st = (disp.mfx_set_config_filter_property)(
                cfg,
                b"mfxImplDescription.VendorID\0".as_ptr(),
                variant_u32(0x8086),
            );
            if st != MFX_ERR_NONE {
                return Err(format!("MFXSetConfigFilterProperty(VendorID) failed: {st}"));
            }
            st = (disp.mfx_set_config_filter_property)(
                cfg,
                b"mfxImplDescription.ApiVersion.Version\0".as_ptr(),
                variant_u32(requested_api_packed()),
            );
            if st != MFX_ERR_NONE {
                return Err(format!(
                    "MFXSetConfigFilterProperty(ApiVersion=2.17, packed=0x{:x}) failed: {st}",
                    requested_api_packed()
                ));
            }
            st = (disp.mfx_set_config_filter_property)(
                cfg,
                b"mfxImplDescription.AccelerationMode\0".as_ptr(),
                variant_u32(MFX_ACCEL_MODE_VIA_D3D11),
            );
            if st != MFX_ERR_NONE {
                return Err(format!("MFXSetConfigFilterProperty(AccelerationMode=D3D11) failed: {st}"));
            }
            // Bind the loader to OUR specific D3D11 device (mirrors FFmpeg's
            // qsv_d3d11_update_config): DeviceID + DeviceLUID + node mask.
            st = (disp.mfx_set_config_filter_property)(
                cfg,
                b"mfxExtendedDeviceId.DeviceID\0".as_ptr(),
                variant_u16(adapter.device as u16),
            );
            if st != MFX_ERR_NONE {
                return Err(format!("MFXSetConfigFilterProperty(DeviceID) failed: {st}"));
            }
            // The LUID must stay alive until MFXCreateSession: keep it in a Box.
            let luid = Box::new(windows::Win32::Foundation::LUID {
                LowPart: adapter.luid_low,
                HighPart: adapter.luid_high,
            });
            st = (disp.mfx_set_config_filter_property)(
                cfg,
                b"mfxExtendedDeviceId.DeviceLUID\0".as_ptr(),
                variant_ptr(&*luid as *const _ as *mut c_void),
            );
            if st != MFX_ERR_NONE {
                return Err(format!("MFXSetConfigFilterProperty(DeviceLUID) failed: {st}"));
            }
            st = (disp.mfx_set_config_filter_property)(
                cfg,
                b"mfxExtendedDeviceId.LUIDDeviceNodeMask\0".as_ptr(),
                variant_u32(0x0001),
            );
            if st != MFX_ERR_NONE {
                return Err(format!("MFXSetConfigFilterProperty(LUIDDeviceNodeMask) failed: {st}"));
            }
            // Create the session via the enumerated implementation (FFmpeg iterates
            // impl_idx through MFXEnumImplementations then MFXCreateSession).
            let mut session: mfxSession = null_mut();
            let mut created: i32 = MFX_ERR_NOT_FOUND;
            let mut impl_idx: u32 = 0;
            while created != MFX_ERR_NONE {
                created = (disp.mfx_create_session)(loader, impl_idx, &mut session);
                if created == MFX_ERR_NONE {
                    break;
                }
                impl_idx += 1;
                if impl_idx > 8 {
                    break;
                }
            }
            drop(luid); // config filters consumed at session creation
            if created != MFX_ERR_NONE {
                return Err(format!("MFXCreateSession failed: {created}"));
            }

            let mut version = mfxVersion { Major: 0, Minor: 0 };
            let _ = (disp.mfx_query_version)(session, &mut version);
            let mut impl_flags: mfxIMPL = 0;
            let impl_rc = (disp.mfx_query_impl)(session, &mut impl_flags);
            eprintln!(
                "SESSION version={}.{} impl_flags=0x{:x} (rc={})",
                version.Major, version.Minor, impl_flags, impl_rc
            );
            // P1.4: does the session already expose a native D3D11 device?
            let mut native_dev: mfxHDL = null_mut();
            let get_rc = (disp.mfx_video_core_get_handle)(
                session,
                MFX_HANDLE_D3D11_DEVICE,
                &mut native_dev,
            );
            eprintln!(
                "GET_HANDLE rc={} native_dev={:p}",
                get_rc, native_dev
            );
            // If oneVPL hands back a device we could adopt it; for now we keep
            // our own D3D11 device (SetHandle below) since rendering, D2D and
            // the compute path already live on it.

            // device handle first, then the allocator — exact FFmpeg order
            // (hwcontext_qsv.c qsv_create_mfx_session: SetHandle, then
            // SetFrameAllocator in ff_qsv_init_session_frames).
            let st = (disp.mfx_video_core_set_handle)(
                session,
                MFX_HANDLE_D3D11_DEVICE,
                windows::core::Interface::as_raw(device) as *mut c_void,
            );
            eprintln!("SET_HANDLE rc={st} dev={:p}", device as *const _ as *const c_void);
            // diagnostic: is the device a valid video device for the runtime?
            let video_dev: windows::core::Result<windows::Win32::Graphics::Direct3D11::ID3D11VideoDevice> =
                device.cast();
            eprintln!("QI ID3D11VideoDevice: {:?}", video_dev.as_ref().map(|_| "ok").map_err(|e| e.to_string()));
            if st != MFX_ERR_NONE {
                return Err(format!("SetHandle(D3D11_DEVICE) failed: {st}"));
            }
            // allocator
            let ctx = Box::into_raw(Box::new(AllocCtx {
                device: device.clone(),
                textures: Vec::new(),
                pairs: Vec::new(),
                pool: Vec::new(),
                records: Vec::new(),
            }));
            let alloc = mfxFrameAllocator {
                reserved: [0; 4],
                pthis: ctx as mfxHDL,
                Alloc: Some(alloc_cb),
                Lock: Some(lock_cb),
                Unlock: Some(unlock_cb),
                GetHDL: Some(gethdl_cb),
                Free: Some(free_cb),
            };
            let st = (disp.mfx_video_core_set_frame_allocator)(session, &alloc);
            if st != MFX_ERR_NONE {
                return Err(format!("SetFrameAllocator failed: {st}"));
            }

            // video params
            let mut p = mfxVideoParam::new();
            {
                let fi = &mut p.mfx.FrameInfo;
                // Env probes: TOASTOVAC_NV12 uses NV12+system memory (the oneVPL
                // hello-encode sample config) to isolate session vs param issues.
                let nv12 = std::env::var("TOASTOVAC_NV12").map(|v| v == "1").unwrap_or(false);
                fi.FourCC = if nv12 { MFX_FOURCC_NV12 } else { MFX_FOURCC_P010 };
                fi.Width = width as u16;
                fi.Height = height as u16;
                fi.CropX = 0;
                fi.CropY = 0;
                fi.CropW = width as u16;
                fi.CropH = height as u16;
                fi.FrameRateExtN = init.fps;
                fi.FrameRateExtD = init.fps_den;
                fi.AspectRatioW = 1;
                fi.AspectRatioH = 1;
                fi.PicStruct = MFX_PICSTRUCT_PROGRESSIVE;
                fi.ChromaFormat = MFX_CHROMAFORMAT_YUV420;
                // CRITICAL: the mfx-gen runtime (hevcehw_base_rext.cpp
                // CheckTargetBitDepth) rejects non-zero BitDepthLuma/Chroma in
                // non-LowPower mode unless they are 0 or 12. FFmpeg leaves them
                // 0 and the runtime derives 10-bit from FourCC=P010. Do NOT
                // set them to 10 — that yields MFX_ERR_INVALID_VIDEO_PARAM.
                fi.BitDepthLuma = 0;
                fi.BitDepthChroma = 0;
            }
            p.mfx.CodecId = MFX_CODEC_HEVC;
            p.mfx.CodecProfile = MFX_PROFILE_HEVC_MAIN10;
            p.mfx.CodecLevel = 0; // auto
            p.mfx.TargetUsage = MFX_TARGETUSAGE_BEST_SPEED;
            p.mfx.GopPicSize = init.gop as u16;
            p.mfx.GopRefDist = 1; // IPPP, no B-frames
            p.mfx.IdrInterval = 1; // every I is IDR (HEVC)
            p.mfx.NumRefFrame = 1;
            // Match FFmpeg's effective QSV encode params (verified via
            // qsv_retrieve_enc_params on this machine): VBR, InitialDelay,
            // modest buffer, NumSlice=2. 4K HEVC on mfx-gen rejects
            // BufferSizeInKB=40000 and NumRefFrame=2.
            p.mfx.RateControlMethod = MFX_RATECONTROL_VBR;
            p.mfx.TargetKbps = init.bitrate_kbps as u16;
            p.mfx.MaxKbps = init.max_kbps as u16;
            // Match FFmpeg's verified effective values exactly: InitialDelayInKB=3750
            // and BufferSizeInKB=5000 at 18 Mbps (qsv_retrieve_enc_params on this
            // machine). Our previous 25%-of-target (4500) was the one RC value that
            // differed from the known-working FFmpeg config.
            p.mfx.InitialDelayInKB = 3750;
            p.mfx.BufferSizeInKB = ((init.bitrate_kbps * 5) / 18) as u16; // 5000 at 18M
            p.mfx.NumSlice = 2;
            p.AsyncDepth = 2;
            // TOASTOVAC_NV12 also switches IOPattern to system memory.
            // Match FFmpeg exactly: IN_VIDEO_MEMORY | OUT_SYSTEM_MEMORY (0x21)
            // for the video-memory path (bitstream lands in system memory),
            // IN_SYSTEM_MEMORY | OUT_SYSTEM_MEMORY (0x22) for the NV12 probe.
            // A bare input-only pattern (0x01) is rejected by mfx-gen's
            // Legacy::QueryIOSurf BLK_CheckIOPattern (-15).
            let nv12 = std::env::var("TOASTOVAC_NV12").map(|v| v == "1").unwrap_or(false);
            p.IOPattern = if nv12 {
                MFX_IOPATTERN_IN_SYSTEM_MEMORY | MFX_IOPATTERN_OUT_SYSTEM_MEMORY
            } else {
                MFX_IOPATTERN_IN_VIDEO_MEMORY | MFX_IOPATTERN_OUT_SYSTEM_MEMORY
            };

            // ext buffers: AUD + repeat PPS + video signal info (BT.2020/PQ)
            let mut opt = mfxExtCodingOption::new();
            opt.Header.BufferId = MFX_EXTBUFF_CODING_OPTION;
            opt.Header.BufferSz = size_of::<mfxExtCodingOption>() as u32;
            opt.AUDelimiter = MFX_CODINGOPTION_ON;
            let mut opt2 = mfxExtCodingOption2::new();
            opt2.Header.BufferId = MFX_EXTBUFF_CODING_OPTION2;
            opt2.Header.BufferSz = size_of::<mfxExtCodingOption2>() as u32;
            opt2.RepeatPPS = MFX_CODINGOPTION_ON;
            let mut vsi = mfxExtVideoSignalInfo::new();
            vsi.Header.BufferId = MFX_EXTBUFF_VIDEO_SIGNAL_INFO;
            vsi.Header.BufferSz = size_of::<mfxExtVideoSignalInfo>() as u32;
            vsi.VideoFormat = 5; // unspecified
            vsi.VideoFullRange = 0; // limited range
            vsi.ColourDescriptionPresent = 1;
            vsi.ColourPrimaries = MFX_COLOR_PRIMARIES_BT2020;
            vsi.TransferCharacteristics = MFX_TRANSFERCHARACTERISTICS_ST2084;
            vsi.MatrixCoefficients = MFX_MATRIXCOEFFICIENTS_BT2020NC;
            // HDR10 mastering display + content light level SEIs (written on IDR
            // frames; the TV uses these to switch to HDR mode — the runtime only
            // emits matrix_coeffs into the VUI, not the full colour description).
            let mut dcvs = mfxExtMasteringDisplayColourVolume::new();
            dcvs.Header.BufferId = MFX_EXTBUFF_MASTERING_DISPLAY_COLOUR_VOLUME;
            dcvs.Header.BufferSz = size_of::<mfxExtMasteringDisplayColourVolume>() as u32;
            dcvs.InsertPayloadToggle = MFX_PAYLOAD_IDR;
            dcvs.DisplayPrimariesX = [35400, 8500, 6550]; // BT.2020 R/G/B x
            dcvs.DisplayPrimariesY = [14600, 39850, 2300]; // BT.2020 R/G/B y
            dcvs.WhitePointX = 15635; // D65
            dcvs.WhitePointY = 16450;
            dcvs.MaxDisplayMasteringLuminance = 1000; // cd/m²
            dcvs.MinDisplayMasteringLuminance = 50; // 0.005 cd/m² (×0.0001)
            let mut clli = mfxExtContentLightLevelInfo::new();
            clli.Header.BufferId = MFX_EXTBUFF_CONTENT_LIGHT_LEVEL_INFO;
            clli.Header.BufferSz = size_of::<mfxExtContentLightLevelInfo>() as u32;
            clli.InsertPayloadToggle = MFX_PAYLOAD_IDR;
            clli.MaxContentLightLevel = 1000;
            clli.MaxPicAverageLightLevel = 400;
            let mut exts: [*mut mfxExtBuffer; 5] = [
                &mut opt as *mut mfxExtCodingOption as *mut mfxExtBuffer,
                &mut opt2 as *mut mfxExtCodingOption2 as *mut mfxExtBuffer,
                &mut vsi as *mut mfxExtVideoSignalInfo as *mut mfxExtBuffer,
                &mut dcvs as *mut mfxExtMasteringDisplayColourVolume as *mut mfxExtBuffer,
                &mut clli as *mut mfxExtContentLightLevelInfo as *mut mfxExtBuffer,
            ];
            // Diagnostic switch: ENV TOASTOVAC_NO_EXT=1 strips all ext buffers
            // to isolate Query/QueryIOSurf behaviour.
            let no_ext = std::env::var("TOASTOVAC_NO_EXT").map(|v| v == "1").unwrap_or(false);
            if no_ext {
                p.ExtParam = std::ptr::null_mut();
                p.NumExtParam = 0;
            } else {
                p.ExtParam = exts.as_mut_ptr();
                p.NumExtParam = 5;
            }

            eprintln!(
                "PARAM sizes vp={} mfx={} fi={} opt={} opt2={} vsi={}",
                size_of::<mfxVideoParam>(),
                size_of::<mfxInfoMFX>(),
                size_of::<mfxFrameInfo>(),
                size_of::<mfxExtCodingOption>(),
                size_of::<mfxExtCodingOption2>(),
                size_of::<mfxExtVideoSignalInfo>()
            );
            eprintln!(
                "PARAM codec=0x{:x} profile={} level={} fps={}/{} w={} h={} rc={} tgt={} max={} buf={} gop={} async={} io=0x{:x}",
                p.mfx.CodecId, p.mfx.CodecProfile, p.mfx.CodecLevel,
                p.mfx.FrameInfo.FrameRateExtN, p.mfx.FrameInfo.FrameRateExtD,
                p.mfx.FrameInfo.Width, p.mfx.FrameInfo.Height,
                p.mfx.RateControlMethod, p.mfx.TargetKbps, p.mfx.MaxKbps, p.mfx.BufferSizeInKB,
                p.mfx.GopPicSize, p.AsyncDepth, p.IOPattern
            );

// Validate params in place (same struct in/out): the Windows mfx-gen
            // runtime returns 0 this way but throws (-> MFX_ERR_NULL_PTR)
            // with a separate output struct (hevcehw_base_legacy.cpp
            // CheckBuffers requires out->NumExtParam == in->NumExtParam; a
            // zeroed output throws and the session wrapper catches it as -2).
            // The in-place call rewrites the capability struct, zeroing
            // IOPattern/NumExtParam, so re-assert those (the ext buffers are
            // app-owned and untouched).
            let skip_query = std::env::var("TOASTOVAC_SKIP_QUERY").map(|v| v == "1").unwrap_or(false);
            if !skip_query {
                let io_before = p.IOPattern;
                let ext_before = (p.NumExtParam, p.ExtParam);
                let qst = (disp.mfx_video_encode_query)(session, &mut p, &mut p);
                eprintln!("QUERY rc={qst} io_after=0x{:x} ext_after={}", p.IOPattern, p.NumExtParam);
                if qst < 0 {
                    return Err(format!("MFXVideoENCODE_Query failed: {qst}"));
                }
                p.IOPattern = io_before;
                p.NumExtParam = ext_before.0;
                p.ExtParam = ext_before.1;
            } else {
                eprintln!("QUERY skipped");
            }
            eprintln!(
                "PARAM-full codec=0x{:x} profile={} level={} thread={} targetUsage={} gop={} refDist={} idr={} rc={} initDelay={} buf={} tgt={} max={} slices={} refs={} fps={}/{} fourcc=0x{:x} chroma={} bitdepth={}/{} w={} h={} crop=({},{},{},{}) async={} io=0x{:x} ext={}",
                p.mfx.CodecId, p.mfx.CodecProfile, p.mfx.CodecLevel, p.mfx.NumThread,
                p.mfx.TargetUsage, p.mfx.GopPicSize, p.mfx.GopRefDist, p.mfx.IdrInterval,
                p.mfx.RateControlMethod, p.mfx.InitialDelayInKB, p.mfx.BufferSizeInKB,
                p.mfx.TargetKbps, p.mfx.MaxKbps, p.mfx.NumSlice, p.mfx.NumRefFrame,
                p.mfx.FrameInfo.FrameRateExtN, p.mfx.FrameInfo.FrameRateExtD,
                p.mfx.FrameInfo.FourCC, p.mfx.FrameInfo.ChromaFormat,
                p.mfx.FrameInfo.BitDepthLuma, p.mfx.FrameInfo.BitDepthChroma,
                p.mfx.FrameInfo.Width, p.mfx.FrameInfo.Height,
                p.mfx.FrameInfo.CropX, p.mfx.FrameInfo.CropY,
                p.mfx.FrameInfo.CropW, p.mfx.FrameInfo.CropH,
                p.AsyncDepth, p.IOPattern, p.NumExtParam
            );
            eprintln!(
                "PARAM-after-query codec=0x{:x} profile={} level={} fps={}/{} w={} h={} rc={} tgt={} max={} buf={} gop={} async={} io=0x{:x}",
                p.mfx.CodecId, p.mfx.CodecProfile, p.mfx.CodecLevel,
                p.mfx.FrameInfo.FrameRateExtN, p.mfx.FrameInfo.FrameRateExtD,
                p.mfx.FrameInfo.Width, p.mfx.FrameInfo.Height,
                p.mfx.RateControlMethod, p.mfx.TargetKbps, p.mfx.MaxKbps, p.mfx.BufferSizeInKB,
                p.mfx.GopPicSize, p.AsyncDepth, p.IOPattern
            );

            // QueryIOSurf for the input surface request. mfx-gen rejects
            // this call with -15 (MFX_ERR_INVALID_VIDEO_PARAM) even for the
            // exact FFmpeg working config (P010 4K30 main10 VBR 18000/28000,
            // 3750/5000, refs=1 slices=2, IOPattern 0x21, no ext). The oneVPL
            // hello-encode sample does not call QueryIOSurf at all, so
            // TOASTOVAC_SKIP_IOSURF=1 uses a fixed pool and goes straight to
            // MFXVideoENCODE_Init.
            let skip_iosurf = std::env::var("TOASTOVAC_SKIP_IOSURF").map(|v| v == "1").unwrap_or(false);
            let want = if skip_iosurf {
                eprintln!("QUERYIOSURF skipped (fixed pool)");
                8
            } else {
                let mut request = mfxFrameAllocRequest::default();
                let st = (disp.mfx_video_encode_query_iosurf)(session, &mut p, &mut request);
                eprintln!(
                    "QUERYIOSURF rc={st} numSuggested={} type=0x{:x}",
                    request.NumFrameSuggested, request.Type
                );
                if st != MFX_ERR_NONE {
                    // This runtime rejects QueryIOSurf (-15) even for the exact
                    // FFmpeg working config; fall back to a fixed pool instead
                    // of failing (oneVPL hello-encode never calls it).
                    eprintln!("QUERYIOSURF rc={st} -> fixed pool (8)");
                    8
                } else {
                    request.NumFrameSuggested.max(6) as usize
                }
            };

            // Input surface pool (P010, UAV-capable). MemId is the raw
            // ID3D11Texture2D* (the internal-allocator convention this runtime
            // uses: custom allocator callbacks are never invoked and
            // MFXMemory_GetSurfaceForEncode is unsupported here, so the app
            // hands the runtime its own textures directly).
            let frame_info = p.mfx.FrameInfo;
            let mut textures = Vec::with_capacity(want);
            let mut surfaces = Vec::with_capacity(want);
            for _ in 0..want {
                let tex = Converter::create_p010_surface(device, width, height)?;
                let ptr = windows::core::Interface::as_raw(&tex) as *mut c_void;
                let mut s = mfxFrameSurface1::default();
                s.Info = frame_info;
                s.Version = MFX_FRAMESURFACE1_VERSION;
                s.Data.MemId = ptr;
                s.Data.MemType = MFX_MEMTYPE_DXVA2_PROCESSOR_TARGET | MFX_MEMTYPE_EXTERNAL_FRAME;
                surfaces.push(s);
                textures.push(tex);
            }

            // init encoder
            let st = (disp.mfx_video_encode_init)(session, &mut p);
            if st != MFX_ERR_NONE && st != MFX_WRN_INCOMPATIBLE_VIDEO_PARAM {
                return Err(format!("MFXVideoENCODE_Init failed: {st}"));
            }

            // query actual params (level)
            let mut actual = mfxVideoParam::new();
            let st2 = (disp.mfx_video_encode_get_video_param)(session, &mut actual);
            let actual_level = if st2 == MFX_ERR_NONE {
                actual.mfx.CodecLevel
            } else {
                0
            };

            // bitstream buffer
            let mut bs_storage = vec![0u8; BITSTREAM_CAP as usize];
            let bs = mfxBitstream::new(bs_storage.as_mut_ptr(), BITSTREAM_CAP);

            Ok(Encoder {
                session,
                _loader: loader,
                device: device.clone(),
                ctx,
                surfaces,
                textures,
                bs,
                bs_storage,
                actual_level,
                api_version: (version.Major, version.Minor),
                impl_name: format!("oneVPL/libvpl hw (api {}.{})", version.Major, version.Minor),
                fps: init.fps,
                fps_den: init.fps_den,
            })
        }
    }

    fn disp(&self) -> Result<Dispatcher, String> {
        Dispatcher::load()
    }

    /// Encode one GPU-resident surface. Returns Some(AU bytes) when an AU
    /// completes for this submission (may be a previous frame's AU).
    pub fn encode_surface(
        &mut self,
        surface_idx: usize,
        frame_index: u64,
        sync_slot: &mut Option<mfxSyncPoint>,
    ) -> Result<Option<Vec<u8>>, String> {
        unsafe {
            let disp = self.disp()?;
            {
                let s = &mut self.surfaces[surface_idx];
                // P5.2: encoder PTS from the same rational rate as the pacing
                // clock — t = frame_index * den / num seconds, in 90 kHz units
                // (90000 ticks/s), integer math: n * 90000 * den / num. For
                // 60000/1001 this yields 1501.5 → 1501/1502 alternating ticks
                // (exact 59.94 cannot be an integer in 90 kHz, but the rate is
                // the rational one, never 1500/60.0).
                let n = frame_index as u128;
                let ticks = (n * 90_000 * self.fps_den as u128) / self.fps as u128;
                s.Data.TimeStamp = ticks as u64;
                let mut ctrl = mfxEncodeCtrl::new();
                let mut sync: mfxSyncPoint = null_mut();
                // Retry on MFX_WRN_DEVICE_BUSY like FFmpeg qsvenc.c (the HW is
                // still working on a previous task; oneVPL 2.x: DEVICE_BUSY=2).
                let mut st: i32 = MFX_ERR_NONE;
                for _attempt in 0..200 {
                    st = (disp.mfx_video_encode_encode_frame_async)(
                        self.session,
                        &mut ctrl,
                        s,
                        &mut self.bs, // REQUIRED non-null (hevcehw_base_impl.cpp EncodeFrameCheck: MFX_CHECK_NULL_PTR2(bs, ...))
                        &mut sync,
                    );
                    if st == MFX_WRN_DEVICE_BUSY {
                        std::thread::sleep(std::time::Duration::from_millis(5));
                        continue;
                    }
                    break;
                }
                match st {
                    MFX_ERR_NONE | MFX_WRN_IN_EXECUTION => {}
                    MFX_ERR_MORE_DATA => return Ok(None),
                    MFX_ERR_MORE_SURFACE => return Ok(None),
                    other => return Err(format!("EncodeFrameAsync failed: {other}")),
                }
                eprintln!("SUBMIT st={st} sync={:p} bsLen={}", sync, self.bs.DataLength);
                if !sync.is_null() {
                    *sync_slot = Some(sync);
                }
            }
            Ok(None)
        }
    }

    /// Sync one outstanding operation and drain the completed AU from the
    /// shared bitstream (classic Media SDK pattern: EncodeFrameAsync wrote the
    /// AU into self.bs; read it out after SyncOperation, then reset).
    pub fn sync_and_get_au(
        &mut self,
        sync: mfxSyncPoint,
        timeout_ms: u32,
    ) -> Result<Option<Vec<u8>>, String> {
        unsafe {
            let disp = self.disp()?;
            eprintln!("SYNC sync={:p} timeout={timeout_ms}", sync);
            let st = (disp.mfx_video_core_sync_operation)(self.session, sync, timeout_ms);
            if st == MFX_ERR_NULL_PTR {
                // This runtime's scheduler occasionally misses the task table
                // for app-submitted syncpoints; fall back to polling the
                // bitstream — the encoder task still writes the AU.
                eprintln!("SYNC table-miss, polling bitstream");
                let deadline =
                    std::time::Instant::now() + std::time::Duration::from_millis(timeout_ms as u64);
                while self.bs.DataLength == 0 && std::time::Instant::now() < deadline {
                    std::thread::sleep(std::time::Duration::from_millis(5));
                }
                eprintln!("POLL done bsLen={}", self.bs.DataLength);
            } else if st != MFX_ERR_NONE && st != MFX_WRN_IN_EXECUTION {
                return Err(format!("SyncOperation failed: {st}"));
            }
            let len = self.bs.DataLength as usize;
            if len == 0 {
                return Ok(None);
            }
            let off = self.bs.DataOffset as usize;
            let cap = self.bs.MaxLength as usize;
            let mut out = Vec::with_capacity(len);
            if off + len <= cap {
                out.extend_from_slice(&self.bs_storage[off..off + len]);
            } else {
                // ring wrap-around
                out.extend_from_slice(&self.bs_storage[off..cap]);
                out.extend_from_slice(&self.bs_storage[0..(off + len) % cap]);
            }
            self.bs.DataLength = 0;
            self.bs.DataOffset = 0;
            Ok(Some(out))
        }
    }

    /// Flush remaining frames from the encoder pipeline.
    pub fn flush(&mut self) -> Result<Vec<u8>, String> {
        unsafe {
            let disp = self.disp()?;
            let mut all = Vec::new();
            for _ in 0..4 {
                let mut sync: mfxSyncPoint = null_mut();
                let st = (disp.mfx_video_encode_encode_frame_async)(
                    self.session,
                    null_mut(),
                    null_mut(),
                    &mut self.bs,
                    &mut sync,
                );
                if st != MFX_ERR_NONE {
                    break;
                }
                if !sync.is_null() {
                    if let Some(au) = self.sync_and_get_au(sync, 2000)? {
                        all.extend_from_slice(&au);
                    }
                }
            }
            Ok(all)
        }
    }

    pub fn close(&mut self) {
        unsafe {
            if let Ok(disp) = self.disp() {
                let _ = (disp.mfx_video_encode_close)(self.session);
                (disp.mfx_unload)(self._loader);
            }
            if !self.ctx.is_null() {
                drop(Box::from_raw(self.ctx));
                self.ctx = null_mut();
            }
        }
    }
}

impl Drop for Encoder {
    fn drop(&mut self) {
        self.close();
    }
}