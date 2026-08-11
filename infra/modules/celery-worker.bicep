@description('Location for all resources')
param location string

@description('Environment name (dev or prod)')
param environment string

@description('Container Apps Environment ID')
param environmentId string

@description('Container Registry Server')
param acrServer string

@description('Container Registry Username')
param acrUsername string = ''

@description('Container Registry Password')
@secure()
param acrPassword string = ''

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
param databaseUrl string

@description('Azure OpenAI Endpoint')
param azureOpenAiEndpoint string = ''

@description('Azure OpenAI Key')
@secure()
param azureOpenAiKey string

@description('WhatsApp API Key')
@secure()
param whatsappApiKey string

@description('WhatsApp From Number')
param whatsappFromNumber string = '917996170801'

@description('WhatsApp Mock Mode')
param whatsappMockMode string = 'false'

@description('WhatsApp Username')
param whatsappUsername string = ''

@description('WhatsApp Password')
@secure()
param whatsappPassword string

@description('GMT Base URL')
param gmtBaseUrl string

@description('GMT Client ID')
param gmtClientId string

@description('GMT Client Secret')
@secure()
param gmtClientSecret string

@description('GMT Username')
param gmtUsername string

@description('GMT Password')
@secure()
param gmtPassword string

@description('GMT Phone')
param gmtPhone string

var appName = 'aiproc-celery-${environment}'

var baseSecrets = [
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
  {
    name: 'whatsapp-password'
    value: whatsappPassword
  }
  {
    name: 'gmt-password'
    value: gmtPassword
  }
  {
    name: 'gmt-client-secret'
    value: gmtClientSecret
  }
]

var acrSecret = !empty(acrPassword) ? [
  {
    name: 'acr-password'
    value: acrPassword
  }
] : []

resource celeryWorkerApp 'Microsoft.App/containerApps@2023-05-01' = {
  name: appName
  location: location
  properties: {
    managedEnvironmentId: environmentId
    configuration: {
      activeRevisionsMode: 'Single'
      ingress: null // Background worker has no HTTP ingress
      registries: !empty(acrPassword) ? [
        {
          server: acrServer
          username: acrUsername
          passwordSecretRef: 'acr-password'
        }
      ] : [
        {
          server: acrServer
          identity: 'system'
        }
      ]
      secrets: concat(baseSecrets, acrSecret)
    }
    template: {
      containers: [
        {
          name: 'celery-worker'
          command: ['celery', '-A', 'app.celery_app', 'worker', '--loglevel=info', '--concurrency=1']
          image: '${acrServer}/${imageTag}'
          env: [
            {
              name: 'PYTHONUNBUFFERED'
              value: '1'
            }
            {
              name: 'LOG_LEVEL'
              value: 'DEBUG'
            }
            {
              name: 'DATABASE_MODE'
              value: 'client'
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
            {
              name: 'WHATSAPP_FROM_NUMBER'
              value: whatsappFromNumber
            }
            {
              name: 'WHATSAPP_MOCK_MODE'
              value: whatsappMockMode
            }
            {
              name: 'WHATSAPP_USERNAME'
              value: whatsappUsername
            }
            {
              name: 'WHATSAPP_PASSWORD'
              secretRef: 'whatsapp-password'
            }
            {
              name: 'GMT_BASE_URL'
              value: gmtBaseUrl
            }
            {
              name: 'GMT_CLIENT_ID'
              value: gmtClientId
            }
            {
              name: 'GMT_CLIENT_SECRET'
              secretRef: 'gmt-client-secret'
            }
            {
              name: 'GMT_USERNAME'
              value: gmtUsername
            }
            {
              name: 'GMT_PASSWORD'
              secretRef: 'gmt-password'
            }
            {
              name: 'GMT_PHONE'
              value: gmtPhone
            }
          ]
          resources: {
            cpu: json('1.0')
            memory: '2.0Gi'
          }
        }
      ]
      scale: {
        minReplicas: minReplicas
        maxReplicas: maxReplicas
        rules: [
          for queueName in [
            'bfs_notification'
            'categorization'
            'seller_matching'
            'report_automation'
            'vector_store'
            'taxonomy_build'
            'maintenance'
            'default'
          ]: {
            name: 'redis-${queueName}-scaling'
            custom: {
              type: 'redis'
              metadata: {
                address: '${redisHost}:6379'
                listLength: '5'
                queueName: queueName
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

resource celeryBeatApp 'Microsoft.App/containerApps@2023-05-01' = {
  name: 'aiproc-celery-beat-${environment}'
  location: location
  properties: {
    managedEnvironmentId: environmentId
    configuration: {
      activeRevisionsMode: 'Single'
      ingress: null
      registries: !empty(acrPassword) ? [
        {
          server: acrServer
          username: acrUsername
          passwordSecretRef: 'acr-password'
        }
      ] : [
        {
          server: acrServer
          identity: 'system'
        }
      ]
      secrets: concat(baseSecrets, acrSecret)
    }
    template: {
      containers: [
        {
          name: 'celery-beat'
          image: '${acrServer}/${imageTag}'
          command: [
            'celery'
            '-A'
            'app.celery_app'
            'beat'
            '--loglevel=info'
            '--schedule=/app/celerybeat/celerybeat-schedule'
          ]
          env: [
            {
              name: 'CELERY_BROKER_URL'
              value: 'redis://${redisHost}:6379/1'
            }
            {
              name: 'CELERY_RESULT_BACKEND'
              value: 'redis://${redisHost}:6379/1'
            }
            {
              name: 'DATABASE_MODE'
              value: 'client'
            }
            {
              name: 'CLIENT_DATABASE_URL'
              secretRef: 'database-url'
            }
          ]
          volumeMounts: [
            {
              volumeName: 'celery-beat-data-vol'
              mountPath: '/app/celerybeat'
            }
          ]
          resources: {
            cpu: json('0.25')
            memory: '0.5Gi'
          }
        }
      ]
      volumes: [
        {
          name: 'celery-beat-data-vol'
          storageName: 'redis-storage'
          storageType: 'AzureFile'
        }
      ]
      scale: {
        minReplicas: 1
        maxReplicas: 1
      }
    }
  }
  identity: {
    type: 'SystemAssigned'
  }
}

output name string = celeryWorkerApp.name
