// Typed failure codes and message helpers (§Failure codes). One code per
// failure; the UI shows the code so the user always knows why a selection
// could not be read or applied.
namespace Toastovac.Everywhere.Host.Protocol;

public static class FailureCodes
{
    public const string NoSelection = "NO_SELECTION";
    public const string EmptySelection = "EMPTY_SELECTION";
    public const string PasswordField = "PASSWORD_FIELD";
    public const string ReadOnlyTarget = "READ_ONLY_TARGET";
    public const string TargetGone = "TARGET_GONE";
    public const string TargetChanged = "TARGET_CHANGED";
    public const string SelectionChanged = "SELECTION_CHANGED";
    public const string DocumentChanged = "DOCUMENT_CHANGED";
    public const string RemoteContextChanged = "REMOTE_CONTEXT_CHANGED";
    public const string ProviderUnavailable = "PROVIDER_UNAVAILABLE";
    public const string HotkeyConflict = "HOTKEY_CONFLICT";
    public const string OcrBackendUnavailable = "OCR_BACKEND_UNAVAILABLE";
    public const string OcrNoText = "OCR_NO_TEXT";
    public const string InputBlockedByUipi = "INPUT_BLOCKED_BY_UIPI";
    public const string ReplaceUnsupported = "REPLACE_UNSUPPORTED";
    public const string ModelUnavailable = "MODEL_UNAVAILABLE";
    public const string ModelCancelled = "MODEL_CANCELLED";
    public const string PipeDisconnected = "PIPE_DISCONNECTED";
}

/// <summary>Replace capabilities a target may expose (§Replace capabilities).</summary>
public static class ReplaceCapability
{
    public const string SemanticRangeEdit = "SEMANTIC_RANGE_EDIT";
    public const string ValuePatternEdit = "VALUE_PATTERN_EDIT";
    public const string UnicodeInputReplace = "UNICODE_INPUT_REPLACE";
    public const string ClipboardTransactionReplace = "CLIPBOARD_TRANSACTION_REPLACE";
    public const string CopyOnly = "COPY_ONLY";
}

/// <summary>Source kinds of the provider hierarchy (§Provider hierarchy).</summary>
public static class SourceKinds
{
    public const string VscodeEditor = "vscode-editor";
    public const string VscodeTerminal = "vscode-terminal";
    public const string Powershell = "powershell";
    public const string WindowsTerminal = "windows-terminal";
    public const string UiaText = "uia-text";
    public const string OcrRegion = "ocr-region";
}
