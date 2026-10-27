[Console]::OutputEncoding=[Text.Encoding]::UTF8
Add-Type @"
using System; using System.Text; using System.Runtime.InteropServices; using System.Collections.Generic;
public class WL { public delegate bool EP(IntPtr h, IntPtr l);
 [DllImport("user32.dll")] public static extern bool EnumWindows(EP f, IntPtr l);
 [DllImport("user32.dll")] public static extern bool EnumChildWindows(IntPtr p, EP f, IntPtr l);
 [DllImport("user32.dll")] public static extern int GetWindowText(IntPtr h, StringBuilder s, int n);
 [DllImport("user32.dll")] public static extern int GetClassName(IntPtr h, StringBuilder s, int n);
 [DllImport("user32.dll")] public static extern uint GetWindowThreadProcessId(IntPtr h, out uint p);
 [DllImport("user32.dll")] public static extern bool IsWindowVisible(IntPtr h);
 public static List<string> Get(uint pid) { var r = new List<string>();
  EnumWindows((h,l)=>{ uint p; GetWindowThreadProcessId(h,out p); if(p==pid){ var t=new StringBuilder(256); var c=new StringBuilder(256);
   GetWindowText(h,t,256); GetClassName(h,c,256); r.Add((IsWindowVisible(h)?"V ":"H ")+c+" | "+t);
   EnumChildWindows(h,(h2,l2)=>{ var t2=new StringBuilder(512); var c2=new StringBuilder(256); GetWindowText(h2,t2,512); GetClassName(h2,c2,256);
     if(t2.Length>0) r.Add("    "+c2+" | "+t2); return true;},IntPtr.Zero);} return true;},IntPtr.Zero); return r; } }
"@
foreach ($p in Get-Process WINWORD,Hwp -ErrorAction SilentlyContinue) { "PID $($p.Id) $($p.ProcessName)"; [WL]::Get([uint32]$p.Id) | Where-Object { $_ -match '^V|^    ' } | Select-Object -First 40 }
