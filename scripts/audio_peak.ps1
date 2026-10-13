# Measure real audio output: master peak of the default speaker and per-app session peaks (5 seconds).
$src = @"
using System;
using System.Collections.Generic;
using System.Runtime.InteropServices;
namespace BL {
  [ComImport, Guid("BCDE0395-E52F-467C-8E3D-C4579291692E")] class MMDeviceEnumerator {}
  [Guid("A95664D2-9614-4F35-A746-DE8DB63617E6"), InterfaceType(ComInterfaceType.InterfaceIsIUnknown)]
  interface IMMDeviceEnumerator { int NotImpl1(); [PreserveSig] int GetDefaultAudioEndpoint(int dataFlow, int role, out IMMDevice dev); }
  [Guid("D666063F-1587-4E43-81F1-B948E807363F"), InterfaceType(ComInterfaceType.InterfaceIsIUnknown)]
  interface IMMDevice { [PreserveSig] int Activate(ref Guid iid, int ctx, IntPtr p, [MarshalAs(UnmanagedType.IUnknown)] out object o); }
  [Guid("C02216F6-8C67-4B5B-9D00-D008E73E0064"), InterfaceType(ComInterfaceType.InterfaceIsIUnknown)]
  interface IAudioMeterInformation { [PreserveSig] int GetPeakValue(out float peak); }
  [Guid("77AA99A0-1BD6-484F-8BC7-2C654C9A9B6F"), InterfaceType(ComInterfaceType.InterfaceIsIUnknown)]
  interface IAudioSessionManager2 { int N1(); int N2(); [PreserveSig] int GetSessionEnumerator(out IAudioSessionEnumerator e); }
  [Guid("E2F5BB11-0570-40CA-ACDD-3AA01277DEE8"), InterfaceType(ComInterfaceType.InterfaceIsIUnknown)]
  interface IAudioSessionEnumerator { [PreserveSig] int GetCount(out int n); [PreserveSig] int GetSession(int i, [MarshalAs(UnmanagedType.IUnknown)] out object s); }
  [Guid("BFB7FF88-7239-4FC9-8FA2-07C950BE9C6D"), InterfaceType(ComInterfaceType.InterfaceIsIUnknown)]
  interface IAudioSessionControl2 { int N1(); int N2(); int N3(); int N4(); int N5(); int N6(); int N7(); int N8(); int N9(); int N10(); int N11(); [PreserveSig] int GetProcessId(out uint pid); }
  public static class Meter {
    public static string Run(int seconds) {
      var en = (IMMDeviceEnumerator)(new MMDeviceEnumerator());
      IMMDevice dev; en.GetDefaultAudioEndpoint(0, 1, out dev);
      object o; Guid g = typeof(IAudioMeterInformation).GUID; dev.Activate(ref g, 23, IntPtr.Zero, out o);
      var master = (IAudioMeterInformation)o;
      Guid g2 = typeof(IAudioSessionManager2).GUID; dev.Activate(ref g2, 23, IntPtr.Zero, out o);
      var mgr = (IAudioSessionManager2)o; IAudioSessionEnumerator se; mgr.GetSessionEnumerator(out se);
      int n; se.GetCount(out n);
      var meters = new List<KeyValuePair<uint, IAudioMeterInformation>>();
      for (int i = 0; i < n; i++) { object s; se.GetSession(i, out s); uint pid; ((IAudioSessionControl2)s).GetProcessId(out pid); meters.Add(new KeyValuePair<uint, IAudioMeterInformation>(pid, (IAudioMeterInformation)s)); }
      float mmax = 0; var smax = new Dictionary<uint, float>();
      for (int t = 0; t < seconds * 20; t++) {
        float p; master.GetPeakValue(out p); if (p > mmax) mmax = p;
        foreach (var kv in meters) { float q; kv.Value.GetPeakValue(out q); float old; smax.TryGetValue(kv.Key, out old); if (q > old || !smax.ContainsKey(kv.Key)) smax[kv.Key] = Math.Max(q, old); }
        System.Threading.Thread.Sleep(50);
      }
      var sb = new System.Text.StringBuilder(); sb.AppendLine("MASTER " + mmax.ToString("0.0000"));
      foreach (var kv in smax) sb.AppendLine("SESSION " + kv.Key + " " + kv.Value.ToString("0.0000"));
      return sb.ToString();
    }
  }
}
"@
Add-Type -TypeDefinition $src -Language CSharp
$out = [BL.Meter]::Run(6)
$edge = @(Get-CimInstance Win32_Process -Filter "Name='msedge.exe'" | Where-Object { $_.CommandLine -match 'band-live\\data\\profile' } | Select-Object -ExpandProperty ProcessId)
foreach ($line in ($out -split "`r?`n" | Where-Object { $_ })) {
  $p = $line -split ' '
  if ($p[0] -eq 'MASTER') { "speaker_master_peak=$($p[1])" }
  else {
    $procId = [int]$p[1]; $name = (Get-Process -Id $procId -ErrorAction SilentlyContinue).ProcessName
    $tag = if ($edge -contains $procId) { ' <== band-live browser' } else { '' }
    "session pid=$procId name=$name peak=$($p[2])$tag"
  }
}
"band-live browser processes: $($edge.Count)"
