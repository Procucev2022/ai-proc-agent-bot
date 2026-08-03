@description('Target environment: dev or prod')
@allowed([
  'dev'
  'prod'
])
param environment string = 'dev'

@description('Azure Region')
param location string = resourceGroup().location

@description('Base application name')
param baseName string = 'aiproc'

@description('Container Registry Server')
param acrServer string

@description('App Image Name and Tag (e.g., aiproc-app:dev-12345)')
param appImageTag string

@description('Celery Worker Image Name and Tag (e.g., aiproc-celery:dev-12345)')
param celeryImageTag string

@description('Database URL')
@secure()
param databaseUrl string = ''

@description('Azure OpenAI Endpoint')
param azureOpenAiEndpoint string = ''

@description('Azure OpenAI Key')
@secure()
param azureOpenAiKey string = ''

@description('WhatsApp API Key')
@secure()
param whatsappApiKey string = ''

// Container Apps Environment
module environmentModule 'modules/container-apps-env.bicep' = {
  name: 'env-deployment'
  params: {
    location: location
    environment: environment
    baseName: baseName
  }
}

// Redis Module
module redisModule 'modules/redis.bicep' = {
  name: 'redis-deployment'
  params: {
    location: location
    environment: environment
    environmentId: environmentModule.outputs.environmentId
  }
}

// ChromaDB Module
module chromaModule 'modules/chroma.bicep' = {
  name: 'chroma-deployment'
  params: {
    location: location
    environment: environment
    environmentId: environmentModule.outputs.environmentId
  }
}

// FastAPI Web App Module
module appModule 'modules/app-service.bicep' = {
  name: 'app-deployment'
  params: {
    location: location
    environment: environment
    environmentId: environmentModule.outputs.environmentId
    acrServer: acrServer
    imageTag: appImageTag
    minReplicas: environment == 'dev' ? 0 : 1
    maxReplicas: environment == 'dev' ? 3 : 10
    redisHost: redisModule.outputs.redisHost
    chromaHost: chromaModule.outputs.chromaHost
    databaseUrl: databaseUrl
    azureOpenAiEndpoint: azureOpenAiEndpoint
    azureOpenAiKey: azureOpenAiKey
    whatsappApiKey: whatsappApiKey
  }
}

// Celery Background Worker Module
module celeryModule 'modules/celery-worker.bicep' = {
  name: 'celery-deployment'
  params: {
    location: location
    environment: environment
    environmentId: environmentModule.outputs.environmentId
    acrServer: acrServer
    imageTag: celeryImageTag
    minReplicas: environment == 'dev' ? 0 : 1
    maxReplicas: environment == 'dev' ? 3 : 10
    redisHost: redisModule.outputs.redisHost
    chromaHost: chromaModule.outputs.chromaHost
    databaseUrl: databaseUrl
    azureOpenAiEndpoint: azureOpenAiEndpoint
    azureOpenAiKey: azureOpenAiKey
  }
}

output appUrl string = appModule.outputs.url
output appFqdn string = appModule.outputs.fqdn
