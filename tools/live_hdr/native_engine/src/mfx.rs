//! oneVPL (Intel VPL) C API FFI — hand-derived from the official
//! intel/libvpl `api/vpl/*.h` headers (v2.17), which are kept in
//! `vpl_api/` for reference. Only the surface subset used by the
//! Toastovač GPU frame engine is bound. The dispatcher (`libvpl.dll`)
//! is loaded dynamically so no import library is required.

#![allow(non_camel_case_types, dead_code, clippy::upper_case_acronyms)]

use std::ffi::c_void;

// ── base types ──────────────────────────────────────────────────────────
pub type mfxU8 = u8;
pub type mfxI8 = i8;
pub type mfxU16 = u16;
pub type mfxI16 = i16;
pub type mfxU32 = u32;
pub type mfxI32 = i32;
pub type mfxU64 = u64;
pub type mfxI64 = i64;
pub type mfxF32 = f32;
pub type mfxHDL = *mut c_void;
pub type mfxMemId = *mut c_void;
pub type mfxStatus = i32;

pub type mfxSession = *mut c_void;
pub type mfxSyncPoint = *mut c_void;
pub type mfxLoader = *mut c_void;
pub type mfxConfig = *mut c_void;
pub type mfxIMPL = i32;

pub const fn MFX_MAKEFOURCC(a: u8, b: u8, c: u8, d: u8) -> u32 {
    ((a as u32) | ((b as u32) << 8) | ((c as u32) << 16) | ((d as u32) << 24))
}

// ── status ──────────────────────────────────────────────────────────────
pub const MFX_ERR_NONE: i32 = 0;
pub const MFX_ERR_UNKNOWN: i32 = -1;
pub const MFX_ERR_NULL_PTR: i32 = -2;
pub const MFX_ERR_UNSUPPORTED: i32 = -3;
pub const MFX_ERR_MEMORY_ALLOC: i32 = -4;
pub const MFX_ERR_NOT_ENOUGH_BUFFER: i32 = -5;
pub const MFX_ERR_INVALID_HANDLE: i32 = -6;
pub const MFX_ERR_LOCK_MEMORY: i32 = -7;
pub const MFX_ERR_NOT_INITIALIZED: i32 = -8;
pub const MFX_ERR_NOT_FOUND: i32 = -9;
pub const MFX_ERR_MORE_DATA: i32 = -10;
pub const MFX_ERR_MORE_SURFACE: i32 = -11;
pub const MFX_ERR_ABORTED: i32 = -12;
pub const MFX_ERR_DEVICE_LOST: i32 = -13;
pub const MFX_ERR_INCOMPATIBLE_VIDEO_PARAM: i32 = -14;
pub const MFX_ERR_INVALID_VIDEO_PARAM: i32 = -15;
pub const MFX_ERR_UNDEFINED_BEHAVIOR: i32 = -16;
pub const MFX_ERR_DEVICE_FAILED: i32 = -17;
pub const MFX_ERR_MORE_BITSTREAM: i32 = -18;
pub const MFX_WRN_IN_EXECUTION: i32 = 1;
pub const MFX_WRN_DEVICE_BUSY: i32 = 2;
pub const MFX_WRN_VIDEO_PARAM_CHANGED: i32 = 3;
pub const MFX_WRN_PARTIAL_ACCELERATION: i32 = 4;
pub const MFX_WRN_INCOMPATIBLE_VIDEO_PARAM: i32 = 5;
pub const MFX_WRN_VALUE_NOT_CHANGED: i32 = 6;
pub const MFX_WRN_OUT_OF_RANGE: i32 = 7;
pub const MFX_ERR_NONE_PARTIAL_OUTPUT: i32 = 12;

// ── fourcc / codec ──────────────────────────────────────────────────────
pub const MFX_FOURCC_P010: u32 = MFX_MAKEFOURCC(b'P', b'0', b'1', b'0');
pub const MFX_FOURCC_NV12: u32 = MFX_MAKEFOURCC(b'N', b'V', b'1', b'2');
pub const MFX_CODEC_HEVC: u32 = MFX_MAKEFOURCC(b'H', b'E', b'V', b'C');

// ── profiles / levels ───────────────────────────────────────────────────
pub const MFX_PROFILE_HEVC_MAIN: u16 = 1;
pub const MFX_PROFILE_HEVC_MAIN10: u16 = 2;

// ── chroma / picstruct ──────────────────────────────────────────────────
pub const MFX_CHROMAFORMAT_YUV420: u16 = 1;
pub const MFX_PICSTRUCT_PROGRESSIVE: u16 = 0x01;

// ── handle types ────────────────────────────────────────────────────────
pub const MFX_HANDLE_D3D11_DEVICE: u32 = 3;

// ── memtype / iopattern ─────────────────────────────────────────────────
pub const MFX_MEMTYPE_INTERNAL_FRAME: u16 = 0x0001;
pub const MFX_MEMTYPE_EXTERNAL_FRAME: u16 = 0x0002;
pub const MFX_MEMTYPE_DXVA2_DECODER_TARGET: u16 = 0x0010;
pub const MFX_MEMTYPE_DXVA2_PROCESSOR_TARGET: u16 = 0x0020;
pub const MFX_MEMTYPE_SYSTEM_MEMORY: u16 = 0x0040;
pub const MFX_MEMTYPE_FROM_ENCODE: u16 = 0x0100;
pub const MFX_MEMTYPE_FROM_VPPIN: u16 = 0x0400;
pub const MFX_MEMTYPE_VIDEO_MEMORY_ENCODER_TARGET: u16 = 0x1000;
pub const MFX_MEMTYPE_VIDEO_MEMORY_UNORDERED_ACCESS: u16 = 0x8000;

pub const MFX_IOPATTERN_IN_VIDEO_MEMORY: u16 = 0x01;
pub const MFX_IOPATTERN_IN_SYSTEM_MEMORY: u16 = 0x02;
pub const MFX_IOPATTERN_OUT_VIDEO_MEMORY: u16 = 0x10;
pub const MFX_IOPATTERN_OUT_SYSTEM_MEMORY: u16 = 0x20;

// ── coding options ──────────────────────────────────────────────────────
pub const MFX_CODINGOPTION_UNKNOWN: u16 = 0;
pub const MFX_CODINGOPTION_ON: u16 = 0x10;
pub const MFX_CODINGOPTION_OFF: u16 = 0x20;

// ── rate control ────────────────────────────────────────────────────────
pub const MFX_RATECONTROL_CBR: u16 = 1;
pub const MFX_RATECONTROL_VBR: u16 = 2;
pub const MFX_RATECONTROL_AVBR: u16 = 4;

pub const MFX_TARGETUSAGE_BEST_SPEED: u16 = 7;
pub const MFX_GOP_CLOSED: u16 = 1;

// ── frametype ───────────────────────────────────────────────────────────
pub const MFX_FRAMETYPE_I: u16 = 0x0001;
pub const MFX_FRAMETYPE_P: u16 = 0x0002;
pub const MFX_FRAMETYPE_IDR: u16 = 0x0080;
pub const MFX_TIMESTAMP_UNKNOWN: u64 = u64::MAX; // -1 as u64

// ── ext buffers ─────────────────────────────────────────────────────────
pub const MFX_EXTBUFF_CODING_OPTION: u32 = MFX_MAKEFOURCC(b'C', b'D', b'O', b'P');
pub const MFX_EXTBUFF_CODING_OPTION2: u32 = MFX_MAKEFOURCC(b'C', b'D', b'O', b'2');
pub const MFX_EXTBUFF_VIDEO_SIGNAL_INFO: u32 = MFX_MAKEFOURCC(b'V', b'S', b'I', b'N');
pub const MFX_EXTBUFF_MASTERING_DISPLAY_COLOUR_VOLUME: u32 = MFX_MAKEFOURCC(b'D', b'C', b'V', b'S');
pub const MFX_EXTBUFF_CONTENT_LIGHT_LEVEL_INFO: u32 = MFX_MAKEFOURCC(b'C', b'L', b'L', b'I');

// InsertHDRPayload enumerator (mfxstructures.h)
pub const MFX_PAYLOAD_OFF: u16 = 0;
pub const MFX_PAYLOAD_IDR: u16 = 1;

// ── impl type (mfxImplDescription filter) ───────────────────────────────
pub const MFX_IMPL_TYPE_SOFTWARE: u32 = 0x0001;
pub const MFX_IMPL_TYPE_HARDWARE: u32 = 0x0002;
pub const MFX_IMPL_TYPE_HARDWARE2: u32 = 0x0003;
pub const MFX_IMPL_TYPE_HARDWARE3: u32 = 0x0004;
pub const MFX_IMPL_TYPE_HARDWARE4: u32 = 0x0005;

pub const MFX_ACCEL_MODE_VIA_D3D9: u32 = 0x0200;
pub const MFX_ACCEL_MODE_VIA_D3D11: u32 = 0x0300;
pub const MFX_ACCEL_MODE_VIA_VAAPI: u32 = 0x0400;

// ── structure version ───────────────────────────────────────────────────
pub const MFX_FRAMESURFACE1_VERSION: u16 = 256 * 1 + 1; // MFX_STRUCT_VERSION(1,1)

// ── vui colour values (ITU-T H.265 VUI / H.264 VUI) ─────────────────────
// Same numeric values as the proven FFmpeg flags:
//   colour_primaries=9  transfer_characteristics=16  matrix_coefficients=9
pub const MFX_COLOR_PRIMARIES_BT709: u16 = 1;
pub const MFX_COLOR_PRIMARIES_BT2020: u16 = 9;
pub const MFX_TRANSFERCHARACTERISTICS_BT709: u16 = 1;
pub const MFX_TRANSFERCHARACTERISTICS_ST2084: u16 = 16;
pub const MFX_MATRIXCOEFFICIENTS_BT709: u16 = 1;
pub const MFX_MATRIXCOEFFICIENTS_BT2020NC: u16 = 9;

// ── structs ─────────────────────────────────────────────────────────────

/// mfxExtBuffer (mfxcommon.h)
#[repr(C)]
#[derive(Clone, Copy, Default)]
pub struct mfxExtBuffer {
    pub BufferId: u32,
    pub BufferSz: u32,
}

/// mfxVersion (mfxcommon.h) — union { struct { Minor, Major }, Version }
#[repr(C)]
#[derive(Clone, Copy, Default)]
pub struct mfxVersion {
    pub Minor: u16,
    pub Major: u16,
}

/// mfxFrameId (mfxstructures.h) — 4 x u16
#[repr(C)]
#[derive(Clone, Copy, Default)]
pub struct mfxFrameId {
    pub TemporalId: u16,
    pub PriorityId: u16,
    pub DependencyId: u16,
    pub QualityId: u16,
}

/// mfxFrameInfo (mfxstructures.h, 2.17) — pack(4), 68 bytes.
/// The Width..CropH group is an anonymous union with a
/// { BufferSize: u64, reserved5: u32 } branch — 12 bytes either way under
/// pack(4), so the flat u16 field sequence is ABI-equivalent.
#[repr(C)]
#[derive(Clone, Copy)]
pub struct mfxFrameInfo {
    pub reserved: [u32; 4],
    pub ChannelId: u16,
    pub BitDepthLuma: u16,
    pub BitDepthChroma: u16,
    pub Shift: u16,
    pub FrameId: mfxFrameId,
    pub FourCC: u32,
    pub Width: u16,
    pub Height: u16,
    pub CropX: u16,
    pub CropY: u16,
    pub CropW: u16,
    pub CropH: u16,
    pub FrameRateExtN: u32,
    pub FrameRateExtD: u32,
    pub reserved3: u16,
    pub AspectRatioW: u16,
    pub AspectRatioH: u16,
    pub PicStruct: u16,
    pub ChromaFormat: u16,
    pub reserved2: u16,
}

impl Default for mfxFrameInfo {
    fn default() -> Self {
        unsafe { std::mem::zeroed() }
    }
}

/// mfxFrameData (mfxstructures.h) — 8-byte alignment.
#[repr(C)]
#[derive(Clone, Copy, Default)]
pub struct mfxFrameData {
    pub ExtParam: *mut *mut mfxExtBuffer, // union w/ reserved2: u64
    pub NumExtParam: u16,
    pub reserved: [u16; 9],
    pub MemType: u16,
    pub PitchHigh: u16,
    pub TimeStamp: u64,
    pub FrameOrder: u32,
    pub Locked: u16,
    pub Pitch: u16,
    pub Y: *mut u8,      // union { Y/R/Y16 }
    pub UV: *mut u8,     // union { UV/VU/CbCr/CrCb/Cb/U/G/Y410/Y416/U16 }
    pub V: *mut u8,      // union { Cr/V/V16/B/A2RGB10/ABGRFP16 }
    pub A: *mut u8,
    pub MemId: mfxMemId,
    pub Corrupted: u16,
    pub DataFlag: u16,
}

/// mfxFrameSurface1 (mfxstructures.h)
#[repr(C)]
#[derive(Clone, Copy)]
pub struct mfxFrameSurface1 {
    pub FrameInterface: *mut c_void, // union w/ reserved[2]
    pub Version: u16,                // mfxStructVersion.Version
    pub reserved1: [u16; 3],
    pub Info: mfxFrameInfo,
    pub Data: mfxFrameData,
}

impl Default for mfxFrameSurface1 {
    fn default() -> Self {
        unsafe { std::mem::zeroed() }
    }
}

/// mfxBitstream (mfxcommon.h)
#[repr(C)]
pub struct mfxBitstream {
    pub EncryptedData: *mut c_void, // union start
    pub ExtParam: *mut *mut mfxExtBuffer,
    pub NumExtParam: u16,
    pub reserved1: u16,
    pub CodecId: u32,
    pub DecodeTimeStamp: i64,
    pub TimeStamp: u64,
    pub Data: *mut u8,
    pub DataOffset: u32,
    pub DataLength: u32,
    pub MaxLength: u32,
    pub PicStruct: u16,
    pub FrameType: u16,
    pub DataFlag: u16,
    pub reserved2: u16,
}

impl mfxBitstream {
    pub fn new(data: *mut u8, max: u32) -> Self {
        mfxBitstream {
            EncryptedData: std::ptr::null_mut(),
            ExtParam: std::ptr::null_mut(),
            NumExtParam: 0,
            reserved1: 0,
            CodecId: 0,
            DecodeTimeStamp: 0,
            TimeStamp: MFX_TIMESTAMP_UNKNOWN,
            Data: data,
            DataOffset: 0,
            DataLength: 0,
            MaxLength: max,
            PicStruct: 0,
            FrameType: 0,
            DataFlag: 0,
            reserved2: 0,
        }
    }
}

/// mfxInfoMFX (mfxstructures.h) — the encode branch of the union.
#[repr(C)]
pub struct mfxInfoMFX {
    pub reserved: [u32; 7],
    pub LowPower: u16,
    pub BRCParamMultiplier: u16,
    pub FrameInfo: mfxFrameInfo,
    pub CodecId: u32,
    pub CodecProfile: u16,
    pub CodecLevel: u16,
    pub NumThread: u16,
    // union (encode opts) — 13 x u16
    pub TargetUsage: u16,
    pub GopPicSize: u16,
    pub GopRefDist: u16,
    pub GopOptFlag: u16,
    pub IdrInterval: u16,
    pub RateControlMethod: u16,
    pub InitialDelayInKB: u16, // union { InitialDelayInKB, QPI, Accuracy }
    pub BufferSizeInKB: u16,
    pub TargetKbps: u16, // union { TargetKbps, QPP, ICQQuality }
    pub MaxKbps: u16,    // union { MaxKbps, QPB, Convergence }
    pub NumSlice: u16,
    pub NumRefFrame: u16,
    pub EncodedOrder: u16,
}

impl mfxInfoMFX {
    pub fn new() -> Self {
        unsafe { std::mem::zeroed() }
    }
}

/// mfxVideoParam (mfxstructures.h, 2.17) — pack(8), 176 bytes.
/// NOTE: oneVPL 2.17 starts with AllocId directly (no reserved[8] prefix;
/// that prefix exists only in older Media SDK headers). Do not re-add it.
#[repr(C)]
pub struct mfxVideoParam {
    pub AllocId: u32,
    pub reserved: [u32; 2],
    pub reserved3: u16,
    pub AsyncDepth: u16,
    pub mfx: mfxInfoMFX,
    pub Protected: u16,
    pub IOPattern: u16,
    pub ExtParam: *mut *mut mfxExtBuffer,
    pub NumExtParam: u16,
    pub reserved2: u16,
}

impl mfxVideoParam {
    pub fn new() -> Self {
        unsafe { std::mem::zeroed() }
    }
}

/// mfxEncodeCtrl (mfxstructures.h)
#[repr(C)]
pub struct mfxEncodeCtrl {
    pub Header: mfxExtBuffer,
    pub reserved: [u32; 4],
    pub reserved1: u16,
    pub MfxNalUnitType: u16,
    pub SkipFrame: u16,
    pub QP: u16,
    pub FrameType: u16,
    pub NumExtParam: u16,
    pub NumPayload: u16,
    pub reserved2: u16,
    pub ExtParam: *mut *mut mfxExtBuffer,
    pub Payload: *mut *mut c_void,
}

impl mfxEncodeCtrl {
    pub fn new() -> Self {
        unsafe { std::mem::zeroed() }
    }
}

/// mfxPayload (mfxstructures.h)
#[repr(C)]
pub struct mfxPayload {
    pub CtrlFlags: u32,
    pub reserved: [u32; 3],
    pub Data: *mut u8,
    pub NumBit: u32,
    pub Type: u16,
    pub BufSize: u16,
}

/// mfxFrameAllocRequest (mfxstructures.h)
#[repr(C)]
#[derive(Clone, Copy)]
pub struct mfxFrameAllocRequest {
    pub AllocId: u32, // union w/ reserved[1]
    pub reserved3: [u32; 3],
    pub Info: mfxFrameInfo,
    pub Type: u16,
    pub NumFrameMin: u16,
    pub NumFrameSuggested: u16,
    pub reserved2: u16,
}

impl Default for mfxFrameAllocRequest {
    fn default() -> Self {
        unsafe { std::mem::zeroed() }
    }
}

/// mfxFrameAllocResponse (mfxstructures.h)
#[repr(C)]
#[derive(Clone, Copy)]
pub struct mfxFrameAllocResponse {
    pub AllocId: u32,
    pub reserved: [u32; 3],
    pub mids: *mut mfxMemId,
    pub NumFrameActual: u16,
    pub reserved2: u16,
}

impl Default for mfxFrameAllocResponse {
    fn default() -> Self {
        unsafe { std::mem::zeroed() }
    }
}

/// mfxExtCodingOption (mfxstructures.h, 2.17) — pack(4), 64 bytes.
/// Field order verified against the 2.17 header: reserved1, MVSearchWindow
/// (i16 pair) and reserved2[2] exist and must not be dropped.
#[repr(C)]
pub struct mfxExtCodingOption {
    pub Header: mfxExtBuffer,
    pub reserved1: u16,
    pub RateDistortionOpt: u16,
    pub MECostType: u16,
    pub MESearchType: u16,
    pub MVSearchWindow: [i16; 2],
    pub EndOfSequence: u16,
    pub FramePicture: u16,
    pub CAVLC: u16,
    pub reserved2: [u16; 2],
    pub RecoveryPointSEI: u16,
    pub ViewOutput: u16,
    pub NalHrdConformance: u16,
    pub SingleSeiNalUnit: u16,
    pub VuiVclHrdParameters: u16,
    pub RefPicListReordering: u16,
    pub ResetRefList: u16,
    pub RefPicMarkRep: u16,
    pub FieldOutput: u16,
    pub IntraPredBlockSize: u16,
    pub InterPredBlockSize: u16,
    pub MVPrecision: u16,
    pub MaxDecFrameBuffering: u16,
    pub AUDelimiter: u16,
    pub EndOfStream: u16,
    pub PicTimingSEI: u16,
    pub VuiNalHrdParameters: u16,
}

impl mfxExtCodingOption {
    pub fn new() -> Self {
        unsafe { std::mem::zeroed() }
    }
}

/// mfxExtCodingOption2 (mfxstructures.h) — fields through UseRawRef.
#[repr(C)]
pub struct mfxExtCodingOption2 {
    pub Header: mfxExtBuffer,
    pub IntRefType: u16,
    pub IntRefCycleSize: u16,
    pub IntRefQPDelta: i16,
    pub MaxFrameSize: u32,
    pub MaxSliceSize: u32,
    pub BitrateLimit: u16,
    pub MBBRC: u16,
    pub ExtBRC: u16,
    pub LookAheadDepth: u16,
    pub Trellis: u16,
    pub RepeatPPS: u16,
    pub BRefType: u16,
    pub AdaptiveI: u16,
    pub AdaptiveB: u16,
    pub LookAheadDS: u16,
    pub NumMbPerSlice: u16,
    pub SkipFrame: u16,
    pub MinQPI: u8,
    pub MaxQPI: u8,
    pub MinQPP: u8,
    pub MaxQPP: u8,
    pub MinQPB: u8,
    pub MaxQPB: u8,
    pub FixedFrameRate: u16,
    pub DisableDeblockingIdc: u16,
    pub DisableVUI: u16,
    pub BufferingPeriodSEI: u16,
    pub EnableMAD: u16,
    pub UseRawRef: u16,
}

impl mfxExtCodingOption2 {
    pub fn new() -> Self {
        unsafe { std::mem::zeroed() }
    }
}

/// mfxExtVideoSignalInfo (mfxstructures.h)
#[repr(C)]
pub struct mfxExtVideoSignalInfo {
    pub Header: mfxExtBuffer,
    pub VideoFormat: u16,
    pub VideoFullRange: u16,
    pub ColourDescriptionPresent: u16,
    pub ColourPrimaries: u16,
    pub TransferCharacteristics: u16,
    pub MatrixCoefficients: u16,
}

impl mfxExtVideoSignalInfo {
    pub fn new() -> Self {
        unsafe { std::mem::zeroed() }
    }
}

/// mfxExtMasteringDisplayColourVolume (mfxstructures.h) — 64 bytes.
/// Coordinates in units of 0.00002, luminance in cd/m² (max) / 0.0001 cd/m² (min).
#[repr(C)]
pub struct mfxExtMasteringDisplayColourVolume {
    pub Header: mfxExtBuffer,
    pub reserved: [u16; 15],
    pub InsertPayloadToggle: u16,
    pub DisplayPrimariesX: [u16; 3],
    pub DisplayPrimariesY: [u16; 3],
    pub WhitePointX: u16,
    pub WhitePointY: u16,
    pub MaxDisplayMasteringLuminance: u32,
    pub MinDisplayMasteringLuminance: u32,
}

impl mfxExtMasteringDisplayColourVolume {
    pub fn new() -> Self {
        unsafe { std::mem::zeroed() }
    }
}

/// mfxExtContentLightLevelInfo (mfxstructures.h) — 32 bytes.
#[repr(C)]
pub struct mfxExtContentLightLevelInfo {
    pub Header: mfxExtBuffer,
    pub reserved: [u16; 9],
    pub InsertPayloadToggle: u16,
    pub MaxContentLightLevel: u16,
    pub MaxPicAverageLightLevel: u16,
}

impl mfxExtContentLightLevelInfo {
    pub fn new() -> Self {
        unsafe { std::mem::zeroed() }
    }
}

/// mfxFrameAllocator (mfxvideo.h) — 64 bytes on x64.
#[repr(C)]
#[derive(Clone, Copy)]
pub struct mfxFrameAllocator {
    pub reserved: [u32; 4],
    pub pthis: mfxHDL,
    pub Alloc: Option<unsafe extern "C" fn(
        mfxHDL,
        *mut mfxFrameAllocRequest,
        *mut mfxFrameAllocResponse,
    ) -> i32>,
    pub Lock: Option<unsafe extern "C" fn(mfxHDL, mfxMemId, *mut mfxFrameData) -> i32>,
    pub Unlock: Option<unsafe extern "C" fn(mfxHDL, mfxMemId, *mut mfxFrameData) -> i32>,
    pub GetHDL: Option<unsafe extern "C" fn(mfxHDL, mfxMemId, *mut mfxHDL) -> i32>,
    pub Free: Option<unsafe extern "C" fn(mfxHDL, *mut mfxFrameAllocResponse) -> i32>,
}

impl Default for mfxFrameAllocator {
    fn default() -> Self {
        unsafe { std::mem::zeroed() }
    }
}

/// mfxHDLPair — GetHDL for D3D11 returns { first: ID3D11Texture2D*, second: array index }
#[repr(C)]
#[derive(Clone, Copy)]
pub struct mfxHDLPair {
    pub first: mfxHDL,
    pub second: mfxHDL,
}

// ── dispatcher (dynamic load of libvpl.dll) ─────────────────────────────

use windows::core::{PCSTR, PCWSTR};
use windows::Win32::Foundation::{HMODULE, HINSTANCE};

pub struct Dispatcher {
    _lib: HMODULE,
    pub mfx_load: unsafe extern "C" fn() -> mfxLoader,
    pub mfx_unload: unsafe extern "C" fn(mfxLoader),
    pub mfx_create_config: unsafe extern "C" fn(mfxLoader) -> mfxConfig,
    pub mfx_enum_implementations:
        unsafe extern "C" fn(mfxLoader, u32, u32, *mut mfxHDL) -> i32,
    pub mfx_disp_release_impl_description: unsafe extern "C" fn(mfxLoader, mfxHDL) -> i32,
    pub mfx_set_config_filter_property:
        unsafe extern "C" fn(mfxConfig, *const u8, mfxVariant) -> i32,
    pub mfx_create_session: unsafe extern "C" fn(mfxLoader, u32, *mut mfxSession) -> i32,
    pub mfx_video_core_set_handle: unsafe extern "C" fn(mfxSession, u32, mfxHDL) -> i32,
    pub mfx_video_core_set_frame_allocator:
        unsafe extern "C" fn(mfxSession, *const mfxFrameAllocator) -> i32,
    pub mfx_video_core_sync_operation:
        unsafe extern "C" fn(mfxSession, mfxSyncPoint, u32) -> i32,
    pub mfx_video_encode_init:
        unsafe extern "C" fn(mfxSession, *mut mfxVideoParam) -> i32,
    pub mfx_video_encode_query:
        unsafe extern "C" fn(mfxSession, *mut mfxVideoParam, *mut mfxVideoParam) -> i32,
    pub mfx_video_encode_query_iosurf:
        unsafe extern "C" fn(mfxSession, *mut mfxVideoParam, *mut mfxFrameAllocRequest) -> i32,
    pub mfx_video_encode_encode_frame_async:
        unsafe extern "C" fn(
            mfxSession,
            *mut mfxEncodeCtrl,
            *mut mfxFrameSurface1,
            *mut mfxBitstream,
            *mut mfxSyncPoint,
        ) -> i32,
    pub mfx_video_encode_get_encode_stat:
        unsafe extern "C" fn(mfxSession, *mut mfxBitstream) -> i32,
    pub mfx_video_encode_get_video_param:
        unsafe extern "C" fn(mfxSession, *mut mfxVideoParam) -> i32,
    pub mfx_memory_get_surface_for_encode:
        unsafe extern "C" fn(mfxSession, *mut *mut mfxFrameSurface1) -> i32,
    pub mfx_video_encode_close: unsafe extern "C" fn(mfxSession) -> i32,
    pub mfx_query_version: unsafe extern "C" fn(mfxSession, *mut mfxVersion) -> i32,
    pub mfx_query_impl: unsafe extern "C" fn(mfxSession, *mut mfxIMPL) -> i32,
    pub mfx_video_core_get_handle: unsafe extern "C" fn(mfxSession, u32, *mut mfxHDL) -> i32,
}

unsafe fn get_proc<T>(lib: HMODULE, name: &[u8]) -> T {
    let addr = windows::Win32::System::LibraryLoader::GetProcAddress(lib, PCSTR(name.as_ptr()));
    match addr {
        Some(f) => std::mem::transmute_copy::<unsafe extern "system" fn() -> isize, T>(&f),
        None => panic!("libvpl.dll missing export {}", String::from_utf8_lossy(name)),
    }
}

impl Dispatcher {
    pub fn load() -> Result<Self, String> {
        unsafe {
            let path: Vec<u16> = "C:\\Windows\\System32\\libvpl.dll\0".encode_utf16().collect();
            let lib: HMODULE = windows::Win32::System::LibraryLoader::LoadLibraryW(PCWSTR(path.as_ptr()))
                .map_err(|e| format!("LoadLibrary libvpl.dll failed: {e}"))?;
            let d = Dispatcher {
                _lib: lib,
                mfx_load: get_proc(lib, b"MFXLoad\0"),
                mfx_unload: get_proc(lib, b"MFXUnload\0"),
                mfx_create_config: get_proc(lib, b"MFXCreateConfig\0"),
                mfx_enum_implementations: get_proc(lib, b"MFXEnumImplementations\0"),
                mfx_disp_release_impl_description: get_proc(lib, b"MFXDispReleaseImplDescription\0"),
                mfx_set_config_filter_property: get_proc(lib, b"MFXSetConfigFilterProperty\0"),
                mfx_create_session: get_proc(lib, b"MFXCreateSession\0"),
                mfx_video_core_set_handle: get_proc(lib, b"MFXVideoCORE_SetHandle\0"),
                mfx_video_core_set_frame_allocator: get_proc(lib, b"MFXVideoCORE_SetFrameAllocator\0"),
                mfx_video_core_sync_operation: get_proc(lib, b"MFXVideoCORE_SyncOperation\0"),
                mfx_video_encode_init: get_proc(lib, b"MFXVideoENCODE_Init\0"),
                mfx_video_encode_query: get_proc(lib, b"MFXVideoENCODE_Query\0"),
                mfx_video_encode_query_iosurf: get_proc(lib, b"MFXVideoENCODE_QueryIOSurf\0"),
                mfx_video_encode_encode_frame_async: get_proc(lib, b"MFXVideoENCODE_EncodeFrameAsync\0"),
                mfx_video_encode_get_encode_stat: get_proc(lib, b"MFXVideoENCODE_GetEncodeStat\0"),
                mfx_video_encode_get_video_param: get_proc(lib, b"MFXVideoENCODE_GetVideoParam\0"),
                mfx_memory_get_surface_for_encode: get_proc(lib, b"MFXMemory_GetSurfaceForEncode\0"),
                mfx_video_encode_close: get_proc(lib, b"MFXVideoENCODE_Close\0"),
                mfx_query_version: get_proc(lib, b"MFXQueryVersion\0"),
                mfx_query_impl: get_proc(lib, b"MFXQueryIMPL\0"),
                mfx_video_core_get_handle: get_proc(lib, b"MFXVideoCORE_GetHandle\0"),
            };
            Ok(d)
        }
    }
}

/// mfxVariant (mfxdefs.h) — { Version: u16, Type: u32, Data: union (8 bytes) }
#[repr(C)]
#[derive(Clone, Copy)]
pub struct mfxVariant {
    pub Version: u16,
    pub pad: u16,
    pub Type: u32,
    pub Data: u64, // union: U32 in low 32 bits; Ptr in low 64 bits
}

pub const MFX_VARIANT_TYPE_U32: u32 = 5; // MFX_DATA_TYPE_U32
pub const MFX_VARIANT_TYPE_U16: u32 = 3; // MFX_DATA_TYPE_U16
pub const MFX_VARIANT_TYPE_PTR: u32 = 11; // MFX_DATA_TYPE_PTR

pub fn variant_u32(v: u32) -> mfxVariant {
    mfxVariant {
        Version: 256 * 1 + 1, // MFX_VARIANT_VERSION
        pad: 0,
        Type: MFX_VARIANT_TYPE_U32,
        Data: v as u64,
    }
}

pub fn variant_u16(v: u16) -> mfxVariant {
    mfxVariant {
        Version: 256 * 1 + 1,
        pad: 0,
        Type: MFX_VARIANT_TYPE_U16,
        Data: v as u64,
    }
}

pub fn variant_ptr(p: *mut c_void) -> mfxVariant {
    mfxVariant {
        Version: 256 * 1 + 1,
        pad: 0,
        Type: MFX_VARIANT_TYPE_PTR,
        Data: p as u64,
    }
}

/// Requested oneVPL API version as a packed mfxVersion.
/// FFmpeg n7.1 derives its qsv device with `mfxVersion ver = { { 3, 1 } }`
/// (implementation version 1.3, packed 0x00010003) as the ApiVersion filter.
/// Overridable via env TOASTOVAC_API_MINOR for probing.
pub const REQUESTED_API_MAJOR: u16 = 1;
pub const REQUESTED_API_MINOR: u16 = 3;
pub fn requested_api_version() -> mfxVersion {
    let minor = std::env::var("TOASTOVAC_API_MINOR")
        .ok()
        .and_then(|v| v.parse().ok())
        .unwrap_or(REQUESTED_API_MINOR);
    mfxVersion {
        Major: REQUESTED_API_MAJOR,
        Minor: minor,
    }
}
/// Packed Version value for the ApiVersion.Version loader filter.
pub fn requested_api_packed() -> u32 {
    let v = requested_api_version();
    (v.Major as u32) << 16 | (v.Minor as u32)
}

/// P0 ABI self-check: print size/align/offsets of every handwritten FFI type
/// so a layout mismatch is caught before it reaches the runtime.
pub fn abi_audit() {
    use std::mem::{align_of, offset_of, size_of};
    eprintln!("=== oneVPL ABI audit (x64, C-ABI) ===");
    let mut row = |name: &str, sz: usize, al: usize| {
        eprintln!("{name:<28} size={sz:<4} align={al}");
    };
    row("mfxStructVersion", 2, 2);
    row("mfxVersion", size_of::<mfxVersion>(), align_of::<mfxVersion>());
    row("mfxVariant", size_of::<mfxVariant>(), align_of::<mfxVariant>());
    row("mfxHDLPair", size_of::<mfxHDLPair>(), align_of::<mfxHDLPair>());
    row("mfxFrameAllocator", size_of::<mfxFrameAllocator>(), align_of::<mfxFrameAllocator>());
    row("mfxFrameInfo", size_of::<mfxFrameInfo>(), align_of::<mfxFrameInfo>());
    row("mfxFrameData", size_of::<mfxFrameData>(), align_of::<mfxFrameData>());
    row("mfxFrameSurface1", size_of::<mfxFrameSurface1>(), align_of::<mfxFrameSurface1>());
    row("mfxVideoParam", size_of::<mfxVideoParam>(), align_of::<mfxVideoParam>());
    row("mfxInfoMFX", size_of::<mfxInfoMFX>(), align_of::<mfxInfoMFX>());
    row("mfxBitstream", size_of::<mfxBitstream>(), align_of::<mfxBitstream>());
    row("mfxEncodeCtrl", size_of::<mfxEncodeCtrl>(), align_of::<mfxEncodeCtrl>());
    row("mfxFrameAllocRequest", size_of::<mfxFrameAllocRequest>(), align_of::<mfxFrameAllocRequest>());
    row("mfxFrameAllocResponse", size_of::<mfxFrameAllocResponse>(), align_of::<mfxFrameAllocResponse>());
    row("mfxPayload", size_of::<mfxPayload>(), align_of::<mfxPayload>());
    row("mfxExtCodingOption", size_of::<mfxExtCodingOption>(), align_of::<mfxExtCodingOption>());
    row("mfxExtCodingOption2", size_of::<mfxExtCodingOption2>(), align_of::<mfxExtCodingOption2>());
    row("mfxExtVideoSignalInfo", size_of::<mfxExtVideoSignalInfo>(), align_of::<mfxExtVideoSignalInfo>());

    eprintln!("-- offsets --");
    eprintln!(
        "mfxVideoParam: AllocId={} AsyncDepth={} mfx={} Protected={} IOPattern={} ExtParam={} NumExtParam={}",
        offset_of!(mfxVideoParam, AllocId),
        offset_of!(mfxVideoParam, AsyncDepth),
        offset_of!(mfxVideoParam, mfx),
        offset_of!(mfxVideoParam, Protected),
        offset_of!(mfxVideoParam, IOPattern),
        offset_of!(mfxVideoParam, ExtParam),
        offset_of!(mfxVideoParam, NumExtParam)
    );
    eprintln!(
        "mfxFrameInfo: FourCC={} Width={} FrameRateExtN={} FrameRateExtD={} AspectRatioW={} ChromaFormat={}",
        offset_of!(mfxFrameInfo, FourCC),
        offset_of!(mfxFrameInfo, Width),
        offset_of!(mfxFrameInfo, FrameRateExtN),
        offset_of!(mfxFrameInfo, FrameRateExtD),
        offset_of!(mfxFrameInfo, AspectRatioW),
        offset_of!(mfxFrameInfo, ChromaFormat)
    );
    eprintln!(
        "mfxExtCodingOption: Header={} AUDelimiter={} EndOfStream={} PicTimingSEI={} VuiNalHrdParameters={}",
        offset_of!(mfxExtCodingOption, Header),
        offset_of!(mfxExtCodingOption, AUDelimiter),
        offset_of!(mfxExtCodingOption, EndOfStream),
        offset_of!(mfxExtCodingOption, PicTimingSEI),
        offset_of!(mfxExtCodingOption, VuiNalHrdParameters)
    );
    eprintln!(
        "mfxFrameSurface1: Version={} Info={} Data={}",
        offset_of!(mfxFrameSurface1, Version),
        offset_of!(mfxFrameSurface1, Info),
        offset_of!(mfxFrameSurface1, Data)
    );
    eprintln!("=== end ABI audit ===");
}