param(
    [Parameter(Mandatory = $true)]
    [long]$ExpectedForegroundHwnd
)

Set-StrictMode -Version Latest
$ErrorActionPreference = "Stop"

[Console]::InputEncoding = New-Object System.Text.UTF8Encoding($false)
$text = [Console]::In.ReadToEnd()
if ([string]::IsNullOrEmpty($text) -or $text.Length -gt 20000) { exit 3 }

Add-Type -TypeDefinition @'
using System;
using System.Runtime.InteropServices;

public static class RnDWorkbenchForegroundTarget
{
    [DllImport("user32.dll")]
    public static extern IntPtr GetForegroundWindow();
}
'@

# The target is frozen when F8 is released. STT may take seconds, so never
# inject into whichever application happens to be focused later.
if (
    $ExpectedForegroundHwnd -le 0 -or
    [RnDWorkbenchForegroundTarget]::GetForegroundWindow().ToInt64() -ne $ExpectedForegroundHwnd
) { exit 9 }

# Refuse password fields and targets which do not expose a text/edit pattern.
# This check is deliberately fail-closed: dictation is never sent as arbitrary
# keystrokes to an unknown foreground control.
Add-Type -AssemblyName UIAutomationClient
$focused = [System.Windows.Automation.AutomationElement]::FocusedElement
if ($null -eq $focused) { exit 5 }
$isPassword = $focused.GetCurrentPropertyValue(
    [System.Windows.Automation.AutomationElement]::IsPasswordProperty,
    $true
)
if ($isPassword -eq $true) { exit 6 }
$isEnabled = $focused.GetCurrentPropertyValue(
    [System.Windows.Automation.AutomationElement]::IsEnabledProperty,
    $true
)
if ($isEnabled -eq $false) { exit 7 }

$supportsText = $false
$valuePattern = $null
if ($focused.TryGetCurrentPattern(
    [System.Windows.Automation.ValuePattern]::Pattern,
    [ref]$valuePattern
)) {
    if ($valuePattern.Current.IsReadOnly) { exit 7 }
    $supportsText = $true
}
$controlType = $focused.GetCurrentPropertyValue(
    [System.Windows.Automation.AutomationElement]::ControlTypeProperty,
    $true
)
if (
    $controlType -eq [System.Windows.Automation.ControlType]::Edit -or
    $controlType -eq [System.Windows.Automation.ControlType]::Document
) {
    $supportsText = $true
} else {
    $pattern = $null
    if ($focused.TryGetCurrentPattern(
        [System.Windows.Automation.TextPattern]::Pattern,
        [ref]$pattern
    )) {
        $supportsText = $true
    }
}
if (-not $supportsText) { exit 7 }

Add-Type -TypeDefinition @"
using System;
using System.Runtime.InteropServices;

public static class RnDWorkbenchUnicodeInput
{
    private const uint InputKeyboard = 1;
    private const uint KeyEventUnicode = 0x0004;
    private const uint KeyEventKeyUp = 0x0002;

    [StructLayout(LayoutKind.Sequential)]
    private struct INPUT
    {
        public uint type;
        public InputUnion data;
    }

    [StructLayout(LayoutKind.Explicit)]
    private struct InputUnion
    {
        [FieldOffset(0)] public KEYBDINPUT keyboard;
    }

    [StructLayout(LayoutKind.Sequential)]
    private struct KEYBDINPUT
    {
        public ushort virtualKey;
        public ushort scanCode;
        public uint flags;
        public uint time;
        public UIntPtr extraInfo;
    }

    [DllImport("user32.dll", SetLastError = true)]
    private static extern uint SendInput(uint count, INPUT[] inputs, int size);

    [DllImport("user32.dll")]
    private static extern IntPtr GetForegroundWindow();

    public static int SendText(string text, long expectedForegroundHwnd)
    {
        foreach (char character in text)
        {
            if (GetForegroundWindow().ToInt64() != expectedForegroundHwnd) return 9;
            INPUT[] inputs = new INPUT[2];
            inputs[0].type = InputKeyboard;
            inputs[0].data.keyboard.scanCode = character;
            inputs[0].data.keyboard.flags = KeyEventUnicode;
            inputs[1] = inputs[0];
            inputs[1].data.keyboard.flags = KeyEventUnicode | KeyEventKeyUp;
            if (SendInput(2, inputs, Marshal.SizeOf(typeof(INPUT))) != 2) return 8;
        }
        return 0;
    }
}
"@

$sendResult = [RnDWorkbenchUnicodeInput]::SendText($text, $ExpectedForegroundHwnd)
exit $sendResult
