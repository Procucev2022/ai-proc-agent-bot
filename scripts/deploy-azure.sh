#!/usr/bin/env bash
# ==============================================================================
# Azure Container Apps (ACA) Deployment Script for AI Procurement Agent
# ==============================================================================

set -e

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
    appImageTag="$IMAGE_TAG_APP" \
    celeryImageTag="$IMAGE_TAG_CELERY" \
  --output table

APP_URL=$(az deployment group show --resource-group "$RESOURCE_GROUP" --name main --query properties.outputs.appUrl.value -o tsv 2>/dev/null || true)

echo "======================================================================"
echo " Deployment Complete!"
echo " App URL: ${APP_URL:-Check Azure Portal for FQDN}"
echo "======================================================================"
