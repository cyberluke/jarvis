@{
    RootModule        = 'Toustovac.TerminalBridge.psm1'
    ModuleVersion     = '1.1.0'
    GUID              = '6f2a1d93-0b77-4c61-9d2e-8a4f3c5e1b70'
    Author            = 'Toustovac'
    Description       = 'First-party terminal-context beacon for the Toustovac Terminal Command Composer.'
    PowerShellVersion = '5.1'
    FunctionsToExport = @('Init-ToustovacTerminalBridge', 'Remove-ToustovacTerminalBridge')
    VariablesToExport = '*'
    CmdletsToExport   = @()
    AliasesToExport   = @()
}
