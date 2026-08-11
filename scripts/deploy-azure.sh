#!/usr/bin/env bash
# ==============================================================================
# Azure Container Apps (ACA) Deployment Script for AI Procurement Agent
# ==============================================================================

set -e

required_env=(DATABASE_URL AZURE_OPENAI_ENDPOINT AZURE_OPENAI_API_KEY WHATSAPP_API_KEY WHATSAPP_USERNAME WHATSAPP_PASSWORD GMT_BASE_URL GMT_CLIENT_ID GMT_CLIENT_SECRET GMT_USERNAME GMT_PASSWORD GMT_PHONE)
for name in "${required_env[@]}"; do
  if [[ -z "${!name:-}" ]]; then echo "Missing required environment variable: $name" >&2; exit 1; fi
done

# Parameters / Defaults
ENV="${1:-dev}"
LOCATION="${2:-centralindia}"
RESOURCE_GROUP="rg-aiproc-${ENV}"
ACR_NAME="acraiproc${ENV}"
BASE_NAME="aiproc"

echo "======================================================================"
echo " Deploying AI Procurement Agent to Azure Container Apps ($ENV)"
echo " Resource Group: $RESOURCE_GROUP | Target Region: $LOCATION"
echo "======================================================================"

# 1. Create or query Resource Group
echo "--> Checking Resource Group..."
if ! az group show --name "$RESOURCE_GROUP" &>/dev/null; then
  echo "Creating Resource Group '$RESOURCE_GROUP' in '$LOCATION'..."
  az group create --name "$RESOURCE_GROUP" --location "$LOCATION" --output table
else
  LOCATION=$(az group show --name "$RESOURCE_GROUP" --query location -o tsv)
  echo "✓ Resource Group '$RESOURCE_GROUP' already exists in location '$LOCATION'."
fi

# 2. Create Azure Container Registry (ACR)
echo "--> Checking Azure Container Registry ($ACR_NAME)..."
if ! az acr show --name "$ACR_NAME" --resource-group "$RESOURCE_GROUP" &>/dev/null; then
  az acr create --resource-group "$RESOURCE_GROUP" --name "$ACR_NAME" --sku Basic --admin-enabled true --output table
else
  echo "✓ ACR '$ACR_NAME' already exists."
fi

# 3. Log in to ACR
echo "--> Logging into ACR..."
az acr login --name "$ACR_NAME"

# 4. Build and Push Container Images
IMAGE_TAG_APP="aiproc-app:${ENV}-latest"
IMAGE_TAG_CELERY="aiproc-celery:${ENV}-latest"

ACR_SERVER=$(az acr show --name "$ACR_NAME" --query loginServer -o tsv)
ACR_USERNAME=$(az acr credential show --name "$ACR_NAME" --query username -o tsv)
ACR_PASSWORD=$(az acr credential show --name "$ACR_NAME" --query "passwords[0].value" -o tsv)
[[ -n "$ACR_SERVER" && -n "$ACR_USERNAME" && -n "$ACR_PASSWORD" ]] || { echo "Unable to obtain ACR credentials" >&2; exit 1; }

echo "--> Building and pushing App Docker Image: ${ACR_SERVER}/${IMAGE_TAG_APP}..."
az acr build --registry "$ACR_NAME" --image "$IMAGE_TAG_APP" --file Dockerfile.app .

echo "--> Building and pushing Celery Docker Image: ${ACR_SERVER}/${IMAGE_TAG_CELERY}..."
az acr build --registry "$ACR_NAME" --image "$IMAGE_TAG_CELERY" --file Dockerfile.celery .

# 5. Deploy Infrastructure via Bicep
echo "--> Deploying Bicep Infrastructure..."
az deployment group create \
  --resource-group "$RESOURCE_GROUP" \
  --template-file infra/main.bicep \
  --parameters \
    environment="$ENV" \
    location="$LOCATION" \
    baseName="$BASE_NAME" \
    acrServer="$ACR_SERVER" \
    acrUsername="$ACR_USERNAME" \
    acrPassword="$ACR_PASSWORD" \
    appImageTag="$IMAGE_TAG_APP" \
    celeryImageTag="$IMAGE_TAG_CELERY" \
    databaseUrl="$DATABASE_URL" \
    azureOpenAiEndpoint="$AZURE_OPENAI_ENDPOINT" \
    azureOpenAiKey="$AZURE_OPENAI_API_KEY" \
    whatsappApiKey="$WHATSAPP_API_KEY" \
    whatsappUsername="$WHATSAPP_USERNAME" \
    whatsappPassword="$WHATSAPP_PASSWORD" \
    gmtBaseUrl="$GMT_BASE_URL" \
    gmtClientId="$GMT_CLIENT_ID" \
    gmtClientSecret="$GMT_CLIENT_SECRET" \
    gmtUsername="$GMT_USERNAME" \
    gmtPassword="$GMT_PASSWORD" \
    gmtPhone="$GMT_PHONE" \
  --output table

APP_URL=$(az deployment group show --resource-group "$RESOURCE_GROUP" --name main --query properties.outputs.appUrl.value -o tsv 2>/dev/null || true)

echo "======================================================================"
echo " Deployment Complete!"
echo " App URL: ${APP_URL:-Check Azure Portal for FQDN}"
echo "======================================================================"
