<#
.SYNOPSIS
    Deploy AI Procurement Agent to Azure Container Apps (ACA)
.PARAMETER Environment
    Target environment: dev or prod (Default: dev)
.PARAMETER Location
    Azure Region (Default: centralindia)
#>

param(
    [string]$Environment = "dev",
    [string]$Location = "centralindia"
)

$ErrorActionPreference = "Stop"

$ResourceGroup = "rg-aiproc-$Environment"
$AcrName = "acraiproc$Environment"
$BaseName = "aiproc"

Write-Host "======================================================================" -ForegroundColor Cyan
Write-Host " Deploying AI Procurement Agent to Azure Container Apps ($Environment)" -ForegroundColor Cyan
Write-Host " Resource Group: $ResourceGroup | Region: $Location" -ForegroundColor Cyan
Write-Host "======================================================================" -ForegroundColor Cyan

# 1. Create Resource Group
Write-Host "--> Creating Resource Group if not exists..." -ForegroundColor Yellow
az group create --name $ResourceGroup --location $Location --output table

# 2. Create ACR
Write-Host "--> Creating Azure Container Registry ($AcrName)..." -ForegroundColor Yellow
az acr create --resource-group $ResourceGroup --name $AcrName --sku Basic --admin-enabled true --output table

# 3. Get ACR Server
$AcrServer = az acr show --name $AcrName --query loginServer -o tsv

# 4. Build and Push Container Images
$ImageTagApp = "aiproc-app:${Environment}-latest"
$ImageTagCelery = "aiproc-celery:${Environment}-latest"

Write-Host "--> Building and pushing App Docker image to ACR..." -ForegroundColor Yellow
az acr build --registry $AcrName --image $ImageTagApp --file Dockerfile.app .

Write-Host "--> Building and pushing Celery Docker image to ACR..." -ForegroundColor Yellow
az acr build --registry $AcrName --image $ImageTagCelery --file Dockerfile.celery .

# 5. Deploy Bicep
Write-Host "--> Deploying Bicep Infrastructure..." -ForegroundColor Yellow
az deployment group create `
  --resource-group $ResourceGroup `
  --template-file infra/main.bicep `
  --parameters `
    environment=$Environment `
    location=$Location `
    baseName=$BaseName `
    acrServer=$AcrServer `
    appImageTag=$ImageTagApp `
    celeryImageTag=$ImageTagCelery `
  --output table

Write-Host "======================================================================" -ForegroundColor Green
Write-Host " Deployment Completed Successfully!" -ForegroundColor Green
Write-Host "======================================================================" -ForegroundColor Green
