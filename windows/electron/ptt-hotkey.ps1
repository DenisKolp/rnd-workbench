param(
    [Parameter(Mandatory = $true)]
    [int]$ParentPid
)

Set-StrictMode -Version Latest
$ErrorActionPreference = "Stop"

# A low-level hook is used instead of GetAsyncKeyState so F8 is an exclusive
# push-to-talk gesture: the foreground application never receives the normal
# F8 command. Auto-repeat is swallowed but emits only one logical key-down.
Add-Type -TypeDefinition @'
using System;
using System.ComponentModel;
using System.Diagnostics;
using System.Runtime.InteropServices;
using System.Threading;

public static class RnDWorkbenchExclusiveHotkey
{
    private const int WhKeyboardLl = 13;
    private const int VkF8 = 0x77;
    private const int WmKeyDown = 0x0100;
    private const int WmKeyUp = 0x0101;
    private const int WmSysKeyDown = 0x0104;
    private const int WmSysKeyUp = 0x0105;
    private const uint WmQuit = 0x0012;

    private static readonly LowLevelKeyboardProc HookCallback = HandleKeyboard;
    private static IntPtr hookHandle = IntPtr.Zero;
    private static bool f8Down;

    private delegate IntPtr LowLevelKeyboardProc(int code, IntPtr message, IntPtr data);

    [StructLayout(LayoutKind.Sequential)]
    private struct KeyboardData
    {
        public uint virtualKey;
        public uint scanCode;
        public uint flags;
        public uint time;
        public UIntPtr extraInfo;
    }

    [StructLayout(LayoutKind.Sequential)]
    private struct Point
    {
        public int x;
        public int y;
    }

    [StructLayout(LayoutKind.Sequential)]
    private struct Message
    {
        public IntPtr window;
        public uint id;
        public UIntPtr wParam;
        public IntPtr lParam;
        public uint time;
        public Point point;
        public uint privateData;
    }

    [DllImport("user32.dll", SetLastError = true)]
    private static extern IntPtr SetWindowsHookEx(
        int hookId,
        LowLevelKeyboardProc callback,
        IntPtr module,
        uint threadId
    );

    [DllImport("user32.dll", SetLastError = true)]
    [return: MarshalAs(UnmanagedType.Bool)]
    private static extern bool UnhookWindowsHookEx(IntPtr hook);

    [DllImport("user32.dll")]
    private static extern IntPtr CallNextHookEx(
        IntPtr hook,
        int code,
        IntPtr message,
        IntPtr data
    );

    [DllImport("user32.dll")]
    private static extern int GetMessage(
        out Message message,
        IntPtr window,
        uint minimum,
        uint maximum
    );

    [DllImport("user32.dll")]
    [return: MarshalAs(UnmanagedType.Bool)]
    private static extern bool TranslateMessage(ref Message message);

    [DllImport("user32.dll")]
    private static extern IntPtr DispatchMessage(ref Message message);

    [DllImport("user32.dll", SetLastError = true)]
    [return: MarshalAs(UnmanagedType.Bool)]
    private static extern bool PostThreadMessage(
        uint threadId,
        uint message,
        UIntPtr wParam,
        IntPtr lParam
    );

    [DllImport("user32.dll")]
    private static extern IntPtr GetForegroundWindow();

    [DllImport("kernel32.dll")]
    private static extern uint GetCurrentThreadId();

    [DllImport("kernel32.dll", CharSet = CharSet.Unicode, SetLastError = true)]
    private static extern IntPtr GetModuleHandle(string moduleName);

    private static IntPtr HandleKeyboard(int code, IntPtr message, IntPtr data)
    {
        if (code >= 0)
        {
            KeyboardData keyboard = (KeyboardData)Marshal.PtrToStructure(
                data,
                typeof(KeyboardData)
            );
            int messageId = message.ToInt32();
            bool isDown = messageId == WmKeyDown || messageId == WmSysKeyDown;
            bool isUp = messageId == WmKeyUp || messageId == WmSysKeyUp;
            if (keyboard.virtualKey == VkF8 && (isDown || isUp))
            {
                if (isDown && !f8Down)
                {
                    f8Down = true;
                    WriteKeyEvent("down");
                }
                else if (isUp && f8Down)
                {
                    f8Down = false;
                    WriteKeyEvent("up");
                }

                // Swallow every physical/repeated F8 transition. This avoids
                // triggering an unrelated F8 command in the active program.
                return new IntPtr(1);
            }
        }
        return CallNextHookEx(hookHandle, code, message, data);
    }

    private static void WriteKeyEvent(string phase)
    {
        long foreground = GetForegroundWindow().ToInt64();
        Console.Out.WriteLine(
            "{\"type\":\"key\",\"key\":\"F8\",\"phase\":\"" +
            phase + "\",\"foreground_hwnd\":" + foreground + "}"
        );
        Console.Out.Flush();
    }

    private static void WatchParent(int parentPid, uint messageThread)
    {
        while (true)
        {
            Thread.Sleep(500);
            try
            {
                using (Process parent = Process.GetProcessById(parentPid))
                {
                    if (!parent.HasExited) continue;
                }
            }
            catch (ArgumentException)
            {
                // The Electron main process no longer exists.
            }
            catch (InvalidOperationException)
            {
                // Treat an unqueryable parent as stopped and remove the hook.
            }
            PostThreadMessage(messageThread, WmQuit, UIntPtr.Zero, IntPtr.Zero);
            return;
        }
    }

    public static void Run(int parentPid)
    {
        if (parentPid <= 0) throw new ArgumentOutOfRangeException("parentPid");
        uint messageThread = GetCurrentThreadId();
        hookHandle = SetWindowsHookEx(
            WhKeyboardLl,
            HookCallback,
            GetModuleHandle(null),
            0
        );
        if (hookHandle == IntPtr.Zero)
        {
            throw new Win32Exception(Marshal.GetLastWin32Error());
        }

        Thread parentWatcher = new Thread(delegate() { WatchParent(parentPid, messageThread); });
        parentWatcher.IsBackground = true;
        parentWatcher.Name = "RnDWorkbenchParentWatch";
        parentWatcher.Start();

        Console.Out.WriteLine(
            "{\"type\":\"ready\",\"key\":\"F8\"," +
            "\"mode\":\"global_exclusive_hold\",\"swallowed\":true}"
        );
        Console.Out.Flush();

        try
        {
            Message message;
            while (GetMessage(out message, IntPtr.Zero, 0, 0) > 0)
            {
                TranslateMessage(ref message);
                DispatchMessage(ref message);
            }
        }
        finally
        {
            if (hookHandle != IntPtr.Zero)
            {
                UnhookWindowsHookEx(hookHandle);
                hookHandle = IntPtr.Zero;
            }
        }
    }
}
'@

[Console]::OutputEncoding = New-Object System.Text.UTF8Encoding($false)
[RnDWorkbenchExclusiveHotkey]::Run($ParentPid)
