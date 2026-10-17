# Checks from PowerShell that SASI's open models answer on Google Cloud Vertex AI.
# Each model gets one tiny question, which costs a fraction of a cent.
#
#   powershell -ExecutionPolicy Bypass -File deploy\check-open-models.ps1
#   powershell -ExecutionPolicy Bypass -File deploy\check-open-models.ps1 -Models qwen/qwen3-coder-480b-a35b-instruct-maas
#
# Needs the Google Cloud CLI (winget install --id Google.CloudSDK -e). Add -Login the first time to sign in.
param(
  [string]$Project = "sasi-511906",
  [string[]]$Models = @("zai-org/glm-5.2-maas", "deepseek-ai/deepseek-v4.1-flash-maas"),
  [switch]$Login
)

if ($Login) { gcloud auth login }
$H = @{ Authorization = "Bearer $(gcloud auth print-access-token)"; "x-goog-user-project" = $Project }
function Why($e) { if ($e.ErrorDetails.Message) { $e.ErrorDetails.Message } else { $e.Exception.Message } }

foreach ($m in $Models) {
  $body = @{ model = $m; max_tokens = 300; messages = @(@{ role = "user"; content = "Reply with the word OK." }) } | ConvertTo-Json -Depth 5
  foreach ($loc in "global", "us-central1") {
    $hostName = if ($loc -eq "global") { "aiplatform.googleapis.com" } else { "$loc-aiplatform.googleapis.com" }
    try {
      $r = Invoke-RestMethod -Method Post -Headers $H -ContentType "application/json" -Body $body `
        -Uri "https://$hostName/v1/projects/$Project/locations/$loc/endpoints/openapi/chat/completions"
      "$m ($loc) WORKS: $($r.choices[0].message.content)"
      break
    } catch { "$m ($loc) failed: $(Why $_)" }
  }
}
