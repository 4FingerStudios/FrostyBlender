# Writes the class and field names a Frosty Editor profile SDK knows, keyed the way
# Battle for Neighborville's SharedTypeDescriptors.ebx names them: class TypeInfo GUIDs
# (in descriptor byte order) and field name hashes. Reads the SDK by reflection only.
param([Parameter(Mandatory)][string]$Frosty, [string]$Profile = 'PVZ3SDK.dll', [Parameter(Mandatory)][string]$Out)
$ErrorActionPreference = 'Stop'
$script:root = $Frosty
$script:tried = @{}
[AppDomain]::CurrentDomain.add_AssemblyResolve([ResolveEventHandler]{
    param($s, $a)
    $n = ($a.Name -split ',')[0]
    if ($n -like '*.resources' -or $script:tried.ContainsKey($n)) { return $null }
    $script:tried[$n] = $true
    foreach ($dir in @($script:root, (Join-Path $script:root 'ThirdParty'), (Join-Path $script:root 'Profiles'))) {
        $p = Join-Path $dir ($n + '.dll')
        if (Test-Path $p) { return [System.Reflection.Assembly]::LoadFrom($p) }
    }
    return $null
})
[void][System.Reflection.Assembly]::LoadFrom((Join-Path $Frosty 'FrostySdk.dll'))
$asm = [System.Reflection.Assembly]::LoadFrom((Join-Path (Join-Path $Frosty 'Profiles') $Profile))
try { $types = $asm.GetTypes() } catch { $types = $_.Exception.Types | Where-Object { $_ -ne $null } }
$flags = [System.Reflection.BindingFlags]'Public,NonPublic,Instance,DeclaredOnly'
$classes = @{}; $fields = @{}; $perClass = @{}
foreach ($t in $types) {
    foreach ($a in $t.GetCustomAttributesData()) {
        if ($a.AttributeType.Name -eq 'TypeInfoGuidAttribute') {
            $bytes = ([Guid][string]$a.ConstructorArguments[0].Value).ToByteArray()
            $classes[(($bytes | ForEach-Object { $_.ToString('x2') }) -join '')] = $t.Name
        }
    }
    $own = @{}
    foreach ($p in $t.GetProperties($flags)) {
        foreach ($a in $p.GetCustomAttributesData()) {
            if ($a.AttributeType.Name -eq 'HashAttribute') {
                $hash = [string](([int64]$a.ConstructorArguments[0].Value + 4294967296) % 4294967296)
                $own[$hash] = $p.Name
                if (-not $fields.ContainsKey($hash)) { $fields[$hash] = $p.Name }
            }
        }
    }
    if ($own.Count) { $perClass[$t.Name] = $own }
}
[ordered]@{ classes = $classes; fields = $fields; class_fields = $perClass } | ConvertTo-Json -Depth 4 -Compress | Out-File -Encoding utf8 $Out
"classes: $($classes.Count) fields: $($fields.Count)"
