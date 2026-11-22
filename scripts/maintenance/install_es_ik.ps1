param([string]$Container = 'elasticsearch-9.3.2')

$ErrorActionPreference = 'Stop'
$dictDir = Join-Path $PSScriptRoot '../../deploy/elasticsearch/ik'
$pluginName = 'analysis-ik'

function Invoke-Docker {
    & docker @args
    if ($LASTEXITCODE -ne 0) { throw "Docker command failed: $($args[0])" }
}

# 按目标容器的镜像版本安装兼容插件。
$version = Invoke-Docker inspect --format '{{index .Config.Labels "org.opencontainers.image.version"}}' $Container
if ($version -notmatch '^\d+\.\d+\.\d+$') { throw 'Cannot determine the Elasticsearch image version.' }
$installed = Invoke-Docker exec $Container /usr/share/elasticsearch/bin/elasticsearch-plugin list
if ($installed -notcontains $pluginName) {
    $zip = Join-Path ([IO.Path]::GetTempPath()) ("tutor-analysis-ik-" + [guid]::NewGuid() + ".zip")
    $containerZip = '/tmp/' + [IO.Path]::GetFileName($zip)
    try {
        $url = "https://get.infini.cloud/elasticsearch/analysis-ik/$version"
        & curl.exe --fail --silent --show-error --location --ssl-revoke-best-effort --retry 2 --max-time 90 $url --output $zip
        if ($LASTEXITCODE -ne 0) { throw "Download of IK $version failed." }
        Invoke-Docker cp $zip "${Container}:$containerZip"
        try {
            Invoke-Docker exec $Container /usr/share/elasticsearch/bin/elasticsearch-plugin install --batch "file://$containerZip"
        } finally {
            Invoke-Docker exec --user 0 $Container rm -f -- $containerZip
        }
    } finally {
        if (Test-Path -LiteralPath $zip) { Remove-Item -LiteralPath $zip }
    }
}

$ikDir = '/usr/share/elasticsearch/config/analysis-ik'
Invoke-Docker exec $Container mkdir -p $ikDir
foreach ($file in @('IKAnalyzer.cfg.xml', 'tutor_math.dic', 'tutor_physics.dic')) {
    Invoke-Docker cp (Join-Path $dictDir $file) "${Container}:$ikDir/$file"
}
Invoke-Docker restart $Container
