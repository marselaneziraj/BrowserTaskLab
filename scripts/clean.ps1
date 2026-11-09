# Run from the repository after stopping the server and deactivating its environment.
[CmdletBinding(SupportsShouldProcess)]
param([switch]$IncludeEnvironment)
$ErrorActionPreference = 'Stop'
$repoRoot = [System.IO.Path]::GetFullPath((Join-Path $PSScriptRoot '..')).TrimEnd('\', '/')
if (-not (Test-Path -LiteralPath (Join-Path $repoRoot 'pyproject.toml'))) {
    throw 'Repository root verification failed.'
}
$targets = @('.runtime', '.pytest_cache', '.ruff_cache', 'build', 'dist', '.coverage', 'htmlcov')
if ($IncludeEnvironment) { $targets += '.venv' }
$paths = @($targets | ForEach-Object { Join-Path $repoRoot $_ })
$paths += @(Get-ChildItem -LiteralPath (Join-Path $repoRoot 'src') -Directory -Recurse -Force |
    Where-Object { $_.Name -eq '__pycache__' -or $_.Name -like '*.egg-info' } |
    Select-Object -ExpandProperty FullName)
$paths += @(Get-ChildItem -LiteralPath (Join-Path $repoRoot 'tests') -Directory -Recurse -Force |
    Where-Object { $_.Name -eq '__pycache__' } | Select-Object -ExpandProperty FullName)
foreach ($candidate in $paths) {
    $resolvedTarget = [System.IO.Path]::GetFullPath($candidate)
    if (-not $resolvedTarget.StartsWith($repoRoot + [System.IO.Path]::DirectorySeparatorChar, [System.StringComparison]::OrdinalIgnoreCase)) {
        throw "Refusing to remove a path outside the repository: $resolvedTarget"
    }
    if ((Test-Path -LiteralPath $resolvedTarget) -and $PSCmdlet.ShouldProcess($resolvedTarget, 'Remove generated files')) {
        Remove-Item -LiteralPath $resolvedTarget -Recurse -Force
    }
}
# .env is deliberately preserved for local use; it remains ignored by Git.
