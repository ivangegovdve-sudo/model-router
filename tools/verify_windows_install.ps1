<#
Install the Windows MSI and exercise its installed GUI with no system Java on PATH.
The app starts with its default local connection; this test sends no paid requests.
#>
[CmdletBinding()]
param(
    [Parameter(Mandatory = $true)][string]$MsiPath,
    [Parameter(Mandatory = $true)][string]$InstallDirectory,
    [Parameter(Mandatory = $true)][string]$ReportDirectory,
    [string]$ExpectedVersion = '1.1.0'
)

Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'
New-Item -ItemType Directory -Path $ReportDirectory -Force | Out-Null
$ReportDirectory = (Resolve-Path -LiteralPath $ReportDirectory).Path
$installLog = Join-Path $ReportDirectory 'msi-install.log'
$result = [ordered]@{
    status = 'running'
    expectedVersion = $ExpectedVersion
    installerExitCode = $null
    installDirectory = $null
    installerContext = $null
    installedVersion = $null
    perUserRegistration = $false
    systemJavaUnavailable = $false
    loadedJvm = $null
    visibleWindow = $false
    aliveSeconds = 0
    cleanExit = $false
}
$app = $null
$installer = $null
$failure = $null

try {
    $msi = (Resolve-Path -LiteralPath $MsiPath).Path
    $requestedDirectory = [IO.Path]::GetFullPath($InstallDirectory)
    $installerArgs = @(
        '/i', ('"{0}"' -f $msi), '/qn', '/norestart',
        '/l*v', ('"{0}"' -f $installLog),
        ('INSTALLDIR="{0}"' -f $requestedDirectory)
    )
    $installer = Start-Process -FilePath (Join-Path $env:WINDIR 'System32\msiexec.exe') `
        -ArgumentList $installerArgs -PassThru
    if (-not $installer.WaitForExit(120000)) {
        throw 'MSI installation did not finish within 120 seconds.'
    }
    $result.installerExitCode = $installer.ExitCode
    if ($installer.ExitCode -notin @(0, 3010)) {
        throw "MSI installation failed with exit code $($installer.ExitCode). See msi-install.log."
    }

    # Per-user MSI products need not have an HKCU Uninstall entry. jpackage
    # writes its ProductCode in HKCU; ask Windows Installer for that product's
    # actual registration context, version and install location.
    $productKey = "HKCU:\Software\Ivan Gegov\ModelRouter\$ExpectedVersion"
    if (-not (Test-Path -LiteralPath $productKey)) {
        throw "No current-user jpackage product key for ModelRouter $ExpectedVersion."
    }
    $productCode = Get-ItemPropertyValue -LiteralPath $productKey -Name 'ProductCode'
    if (-not ('ModelRouterSmoke.WindowsInstaller' -as [type])) {
        Add-Type -TypeDefinition @'
using System.Runtime.InteropServices;
using System.Text;
namespace ModelRouterSmoke {
    public static class WindowsInstaller {
        [DllImport("msi.dll", EntryPoint = "MsiEnumProductsExW", CharSet = CharSet.Unicode, ExactSpelling = true)]
        public static extern uint EnumProducts(string productCode, string userSid,
            uint context, uint index, StringBuilder installedProduct, out uint installedContext,
            StringBuilder installedSid, ref uint sidChars);
        [DllImport("msi.dll", EntryPoint = "MsiGetProductInfoExW", CharSet = CharSet.Unicode, ExactSpelling = true)]
        public static extern uint GetProductInfo(string productCode, string userSid,
            uint context, string property, StringBuilder value, ref uint valueChars);
    }
}
'@
    }
    $installedProduct = [Text.StringBuilder]::new(39)
    $installedSid = [Text.StringBuilder]::new(256)
    [uint32]$context = 0
    [uint32]$sidChars = $installedSid.Capacity
    $currentUserSid = [Security.Principal.WindowsIdentity]::GetCurrent().User.Value
    $queryCode = [ModelRouterSmoke.WindowsInstaller]::EnumProducts(
        $productCode, $currentUserSid, 7, 0, $installedProduct, [ref]$context, $installedSid, [ref]$sidChars
    )
    if ($queryCode -ne 0) { throw "Windows Installer registration lookup failed with status $queryCode." }
    if ($context -notin @(1, 2) -or $installedSid.ToString() -ne $currentUserSid) {
        throw 'Windows Installer did not register the MSI for the current user.'
    }
    $result.installerContext = $context
    $registration = @{}
    foreach ($property in @('InstalledProductName', 'VersionString', 'InstallLocation')) {
        $value = [Text.StringBuilder]::new(4096)
        [uint32]$valueChars = $value.Capacity
        $queryCode = [ModelRouterSmoke.WindowsInstaller]::GetProductInfo(
            $productCode, $currentUserSid, $context, $property, $value, [ref]$valueChars
        )
        if ($queryCode -ne 0) { throw "Windows Installer property $property lookup failed with status $queryCode." }
        $registration[$property] = $value.ToString()
    }
    if ($registration.InstalledProductName -ne 'ModelRouter' -or $registration.VersionString -ne $ExpectedVersion) {
        throw 'Windows Installer registered an unexpected product name or version.'
    }
    $result.installedVersion = $registration.VersionString
    $result.perUserRegistration = $true
    $candidates = @($requestedDirectory)
    if ($registration.InstallLocation) { $candidates += $registration.InstallLocation.Trim('"') }
    $candidates += Join-Path $env:LOCALAPPDATA 'ModelRouter'
    $candidates += Join-Path $env:LOCALAPPDATA 'Programs\ModelRouter'
    $executable = $candidates | ForEach-Object { Join-Path $_ 'ModelRouter.exe' } |
        Where-Object { Test-Path -LiteralPath $_ -PathType Leaf } | Select-Object -First 1
    if (-not $executable) { throw 'Installed ModelRouter.exe was not found.' }
    $installedDirectory = Split-Path -Parent (Resolve-Path -LiteralPath $executable).Path
    $result.installDirectory = $installedDirectory
    if (-not (Test-Path -LiteralPath (Join-Path $installedDirectory 'runtime\bin\server\jvm.dll'))) {
        throw 'The installed application is missing its bundled JVM.'
    }

    $savedPath = $env:PATH
    $savedJavaHomes = @{}
    try {
        foreach ($entry in Get-ChildItem Env: | Where-Object { $_.Name -match '^(JAVA_HOME(_.*)?|JDK_HOME|JRE_HOME)$' }) {
            $savedJavaHomes[$entry.Name] = $entry.Value
            Remove-Item -LiteralPath ("Env:\{0}" -f $entry.Name)
        }
        $env:PATH = "$env:WINDIR\System32;$env:WINDIR;$env:WINDIR\System32\Wbem"
        if (Get-Command java.exe -CommandType Application -ErrorAction SilentlyContinue) {
            throw 'System java.exe is still reachable in the launch environment.'
        }
        $result.systemJavaUnavailable = $true
        $app = Start-Process -FilePath $executable -ArgumentList '--size', '1200x800' `
            -WorkingDirectory $installedDirectory -PassThru `
            -RedirectStandardOutput (Join-Path $ReportDirectory 'app-stdout.log') `
            -RedirectStandardError (Join-Path $ReportDirectory 'app-stderr.log')
    } finally {
        $env:PATH = $savedPath
        foreach ($name in $savedJavaHomes.Keys) { Set-Item -LiteralPath ("Env:\{0}" -f $name) -Value $savedJavaHomes[$name] }
    }

    $startup = [Diagnostics.Stopwatch]::StartNew()
    do {
        $app.Refresh()
        if ($app.HasExited) { throw "Installed app exited during startup with code $($app.ExitCode)." }
        if ($app.MainWindowHandle -ne [IntPtr]::Zero) { break }
        Start-Sleep -Milliseconds 250
    } while ($startup.Elapsed.TotalSeconds -lt 15)
    if ($app.MainWindowHandle -eq [IntPtr]::Zero) { throw 'No GUI window appeared within 15 seconds.' }
    $result.visibleWindow = $true
    $jvm = @($app.Modules | Where-Object { $_.ModuleName -ieq 'jvm.dll' })
    $installedPrefix = $installedDirectory.TrimEnd([IO.Path]::DirectorySeparatorChar) + [IO.Path]::DirectorySeparatorChar
    if ($jvm.Count -ne 1 -or -not $jvm[0].FileName.StartsWith(
        $installedPrefix, [StringComparison]::OrdinalIgnoreCase
    )) { throw 'The GUI did not load jvm.dll from the installed application.' }
    $result.loadedJvm = $jvm[0].FileName

    $alive = [Diagnostics.Stopwatch]::StartNew()
    while ($alive.Elapsed.TotalSeconds -lt 10) {
        $app.Refresh()
        if ($app.HasExited) { throw "The GUI exited before ten seconds with code $($app.ExitCode)." }
        Start-Sleep -Milliseconds 250
    }
    $app.Refresh()
    $result.aliveSeconds = [Math]::Round($alive.Elapsed.TotalSeconds, 2)
    if (-not $app.Responding) { throw 'The installed GUI is not responding.' }
    if (-not $app.CloseMainWindow()) { throw 'Could not request a normal GUI close.' }
    if (-not $app.WaitForExit(10000)) { throw 'The GUI did not exit within ten seconds of a normal close.' }
    if ($app.ExitCode -ne 0) { throw "The GUI closed with exit code $($app.ExitCode)." }
    $result.cleanExit = $true
    $result.status = 'passed'
} catch {
    $failure = $_
    $result.status = 'failed'
    $result.error = $_.Exception.Message
} finally {
    foreach ($process in @($app, $installer)) {
        if ($process) {
            try {
                if (-not $process.HasExited) { $process.Kill(); $process.WaitForExit(5000) | Out-Null }
            } catch { Write-Warning 'A smoke-test process could not be stopped during cleanup.' }
        }
    }
    $result | ConvertTo-Json -Depth 4 | Set-Content -LiteralPath (Join-Path $ReportDirectory 'smoke-result.json') -Encoding utf8
}

if ($failure) { throw $failure }
Write-Host "Windows MSI smoke test passed: per-user install, bundled JVM, visible GUI for $($result.aliveSeconds)s, clean exit."
