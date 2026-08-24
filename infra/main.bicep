@description('Environment name (dev or prod)')
param environment string

@description('Azure Region')
param location string

@description('Base Resource Name')
param baseName string = 'aiproc'

@description('Container Registry Server')
param acrServer string

@description('Container Registry Username')
param acrUsername string = ''

@description('Container Registry Password')
@secure()
param acrPassword string = ''

@description('App Image Tag')
param appImageTag string

@description('Celery Image Tag')
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

@description('WhatsApp From Number')
param whatsappFromNumber string = '917996170801'

@description('WhatsApp Mock Mode')
param whatsappMockMode string = 'false'

@description('WhatsApp Username')
param whatsappUsername string = ''

@description('WhatsApp Password')
@secure()
param whatsappPassword string = ''

@description('GMT Base URL')
param gmtBaseUrl string

@description('GMT Client ID')
param gmtClientId string

@description('GMT Client Secret')
@secure()
param gmtClientSecret string = ''

@description('GMT Username')
param gmtUsername string

@description('GMT Password')
@secure()
param gmtPassword string = ''

@description('GMT Phone')
param gmtPhone string

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
    acrUsername: acrUsername
    acrPassword: acrPassword
    imageTag: appImageTag
    // Never scale the web app to zero. This app is only ever woken by an inbound
    // WhatsApp webhook, so a scaled-to-zero replica means the user's own message
    // pays for the cold start: container activation, image start, interpreter
    // boot, module imports and the readiness probe. Log analysis of
    // 2026-08-04..24 measured that directly - the first message of a
    // conversation took a median of 43.3s to reach the application, against
    // 2.7s for follow-ups, and 37 of the 40 slowest arrivals landed within 21s
    // of a container boot. Keeping one replica warm is what removes that 40s
    // from the first "Hi".
    minReplicas: 1
    maxReplicas: environment == 'dev' ? 3 : 10
    redisHost: redisModule.outputs.redisHost
    chromaHost: chromaModule.outputs.chromaHost
    databaseUrl: databaseUrl
    azureOpenAiEndpoint: azureOpenAiEndpoint
    azureOpenAiKey: azureOpenAiKey
    whatsappApiKey: whatsappApiKey
    whatsappFromNumber: whatsappFromNumber
    whatsappMockMode: whatsappMockMode
    whatsappUsername: whatsappUsername
    whatsappPassword: whatsappPassword
    gmtBaseUrl: gmtBaseUrl
    gmtClientId: gmtClientId
    gmtClientSecret: gmtClientSecret
    gmtUsername: gmtUsername
    gmtPassword: gmtPassword
    gmtPhone: gmtPhone
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
    acrUsername: acrUsername
    acrPassword: acrPassword
    imageTag: celeryImageTag
    minReplicas: environment == 'dev' ? 0 : 1
    maxReplicas: environment == 'dev' ? 3 : 10
    redisHost: redisModule.outputs.redisHost
    chromaHost: chromaModule.outputs.chromaHost
    databaseUrl: databaseUrl
    azureOpenAiEndpoint: azureOpenAiEndpoint
    azureOpenAiKey: azureOpenAiKey
    whatsappApiKey: whatsappApiKey
    whatsappFromNumber: whatsappFromNumber
    whatsappMockMode: whatsappMockMode
    whatsappUsername: whatsappUsername
    whatsappPassword: whatsappPassword
    gmtBaseUrl: gmtBaseUrl
    gmtClientId: gmtClientId
    gmtClientSecret: gmtClientSecret
    gmtUsername: gmtUsername
    gmtPassword: gmtPassword
    gmtPhone: gmtPhone
  }
}

output appUrl string = appModule.outputs.url
output appFqdn string = appModule.outputs.fqdn
