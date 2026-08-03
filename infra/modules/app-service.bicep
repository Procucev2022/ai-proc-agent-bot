@description('Location for all resources')
param location string

@description('Environment name (dev or prod)')
param environment string

@description('Container Apps Environment ID')
param environmentId string

@description('Container Registry Server')
param acrServer string

@description('Container Image Name & Tag')
param imageTag string

@description('Min Replicas (0 for Dev serverless scale-to-zero, 1 for Prod)')
param minReplicas int = 0

@description('Max Replicas')
param maxReplicas int = 5

@description('Redis Host')
param redisHost string

@description('Chroma Host')
param chromaHost string

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

var appName = 'aiproc-app-${environment}'

resource appContainer 'Microsoft.App/containerApps@2023-05-01' = {
  name: appName
  location: location
  properties: {
    managedEnvironmentId: environmentId
    configuration: {
      activeRevisionsMode: 'Single'
      ingress: {
        external: true
        targetPort: 8005
        transport: 'auto'
        allowInsecure: false
      }
      registries: [
        {
          server: acrServer
          identity: 'system'
        }
      ]
      secrets: [
        {
          name: 'database-url'
          value: databaseUrl
        }
        {
          name: 'azure-openai-key'
          value: azureOpenAiKey
        }
        {
          name: 'whatsapp-api-key'
          value: whatsappApiKey
        }
      ]
    }
    template: {
      containers: [
        {
          name: 'app'
          image: '${acrServer}/${imageTag}'
          env: [
            {
              name: 'APP_NAME'
              value: 'AI Procurement Agent (${environment})'
            }
            {
              name: 'REDIS_URL'
              value: 'redis://${redisHost}:6379/0'
            }
            {
              name: 'CELERY_BROKER_URL'
              value: 'redis://${redisHost}:6379/1'
            }
            {
              name: 'CELERY_RESULT_BACKEND'
              value: 'redis://${redisHost}:6379/1'
            }
            {
              name: 'CHROMA_HOST'
              value: chromaHost
            }
            {
              name: 'CHROMA_PORT'
              value: '8000'
            }
            {
              name: 'CHROMA_USE_SERVER'
              value: 'true'
            }
            {
              name: 'CLIENT_DATABASE_URL'
              secretRef: 'database-url'
            }
            {
              name: 'AZURE_OPENAI_ENDPOINT'
              value: azureOpenAiEndpoint
            }
            {
              name: 'AZURE_OPENAI_API_KEY'
              secretRef: 'azure-openai-key'
            }
            {
              name: 'WHATSAPP_API_KEY'
              secretRef: 'whatsapp-api-key'
            }
          ]
          resources: {
            cpu: json('1.0')
            memory: '2.0Gi'
          }
          probes: [
            {
              type: 'Liveness'
              httpGet: {
                path: '/health'
                port: 8005
              }
              initialDelaySeconds: 30
              periodSeconds: 15
            }
            {
              type: 'Readiness'
              httpGet: {
                path: '/health'
                port: 8005
              }
              initialDelaySeconds: 15
              periodSeconds: 10
            }
          ]
        }
      ]
      scale: {
        minReplicas: minReplicas
        maxReplicas: maxReplicas
        rules: [
          {
            name: 'http-scaling-rule'
            http: {
              metadata: {
                concurrentRequests: '50'
              }
            }
          }
        ]
      }
    }
  }
  identity: {
    type: 'SystemAssigned'
  }
}

output fqdn string = appContainer.properties.configuration.ingress.fqdn
output url string = 'https://${appContainer.properties.configuration.ingress.fqdn}'
