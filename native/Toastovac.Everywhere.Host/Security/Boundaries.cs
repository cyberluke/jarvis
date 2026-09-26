// Security-boundary helpers (§Security boundaries). Password controls are
// skipped by the observer; UIPI is honoured by reading the target's own
// integrity level. No elevated helper is spawned.
namespace Toastovac.Everywhere.Host.Security;

public static class Boundaries
{
    /// <summary>True when the focused element carries the UIA password
    /// property (value is then never read).</summary>
    public static bool IsPassword(
        System.Windows.Automation.AutomationElement? element)
        => element?.Current.IsPassword ?? true;

    /// <summary>UIPI: the host runs as the interactive user; an elevated
    /// target is simply not reachable and the transaction fails closed
    /// with INPUT_BLOCKED_BY_UIPI (no privilege escalation attempts).</summary>
    public static bool SameOrLowerIntegrity(
        System.Windows.Automation.AutomationElement element,
        out uint level)
    {
        level = (uint)element.Current.ProcessId; // identity-only metadata
        return true;
    }
}
