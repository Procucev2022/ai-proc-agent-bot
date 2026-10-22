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

@description('GMT Base URL (QUA only)')
param gmtBaseUrl string = ''

@description('GMT Client ID (QUA only)')
param gmtClientId string = ''

@description('GMT Client Secret (QUA only)')
@secure()
param gmtClientSecret string = ''

@description('GMT Username (QUA only)')
param gmtUsername string = ''

@description('GMT Password (QUA only)')
@secure()
param gmtPassword string = ''

@description('GMT Phone (QUA only)')
param gmtPhone string = ''

@description('Procurement backend the bot talks to: qua (QUA Java) or equa (Enterprise QUA bot bridge)')
param backendPlatform string = 'qua'

@description('Shared key for the Enterprise QUA bot bridge (X-Bot-Api-Key); empty on QUA')
@secure()
param botApiKey string = ''

@description('Enterprise QUA bot bridge URL, e.g. https://<backend>/api/bot (EQUA only)')
param equaApiBaseUrl string = ''

@description('EQUA account the bot signs in as (EQUA only)')
param equaServiceUsername string = ''

@description('Mobile of that EQUA account (EQUA only)')
param equaServicePhone string = ''

@description('Portal linked from bot messages on EQUA (optional)')
param equaPortalUrl string = ''

var appName = 'aiproc-celery-${environment}'

// Celery queue names. These must match task_routes plus task_default_queue in
// app/celery_config.py exactly: Celery keys each queue's Redis list by this
// literal name, and that key is what the KEDA scaler reads.
var celeryQueueNames = [
  'bfs_notification'
  'categorization'
  'seller_matching'
  'report_automation'
  'vector_store'
  'taxonomy_build'
  'maintenance'
  'default'
]

// Redis database index the Celery broker uses. Keep in step with
// CELERY_BROKER_URL below (redis://host:6379/1). KEDA defaults this to 0, so
// leaving it unset makes every scaler poll an empty database and never scale.
var celeryBrokerDatabaseIndex = '1'

// One KEDA redis-list scaler per queue.
var celeryScaleRules = [
  for queueName in celeryQueueNames: {
    // A scale rule name must be an RFC 1123 label: lowercase alphanumeric and
    // '-' only. Queue names contain underscores, so sanitise the rule name and
    // keep listName as the literal queue name.
    name: 'redis-${replace(queueName, '_', '-')}-scaling'
    custom: {
      type: 'redis'
      metadata: {
        address: '${redisHost}:6379'
        // The KEDA redis scaler requires 'listName'. 'queueName' is not a
        // recognised parameter and the scaler fails to initialise without it.
        listName: queueName
        listLength: '5'
        databaseIndex: celeryBrokerDatabaseIndex
      }
    }
  }
]

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
]

// BOT_API_KEY is only wired in when one is supplied: a Container Apps secret cannot be empty.
var botApiKeySecret = !empty(botApiKey) ? [
  {
    name: 'bot-api-key'
    value: botApiKey
  }
] : []

var botApiKeyEnv = !empty(botApiKey) ? [
  {
    name: 'BOT_API_KEY'
    secretRef: 'bot-api-key'
  }
] : []

// QUA (GMT_*) and EQUA (EQUA_*) settings are each wired in only when supplied, so
// a deployment for one platform carries nothing for the other — an EQUA
// deployment needs no QUA value at all. A Container Apps secret cannot be empty.
var gmtSecrets = concat(
  empty(gmtPassword) ? [] : [
    {
      name: 'gmt-password'
      value: gmtPassword
    }
  ],
  empty(gmtClientSecret) ? [] : [
    {
      name: 'gmt-client-secret'
      value: gmtClientSecret
    }
  ]
)

var backendEnv = concat(
  empty(gmtBaseUrl) ? [] : [
    {
      name: 'GMT_BASE_URL'
      value: gmtBaseUrl
    }
  ],
  empty(gmtClientId) ? [] : [
    {
      name: 'GMT_CLIENT_ID'
      value: gmtClientId
    }
  ],
  empty(gmtClientSecret) ? [] : [
    {
      name: 'GMT_CLIENT_SECRET'
      secretRef: 'gmt-client-secret'
    }
  ],
  empty(gmtUsername) ? [] : [
    {
      name: 'GMT_USERNAME'
      value: gmtUsername
    }
  ],
  empty(gmtPassword) ? [] : [
    {
      name: 'GMT_PASSWORD'
      secretRef: 'gmt-password'
    }
  ],
  empty(gmtPhone) ? [] : [
    {
      name: 'GMT_PHONE'
      value: gmtPhone
    }
  ],
  empty(equaApiBaseUrl) ? [] : [
    {
      name: 'EQUA_API_BASE_URL'
      value: equaApiBaseUrl
    }
  ],
  empty(equaServiceUsername) ? [] : [
    {
      name: 'EQUA_SERVICE_USERNAME'
      value: equaServiceUsername
    }
  ],
  empty(equaServicePhone) ? [] : [
    {
      name: 'EQUA_SERVICE_PHONE'
      value: equaServicePhone
    }
  ],
  empty(equaPortalUrl) ? [] : [
    {
      name: 'EQUA_PORTAL_URL'
      value: equaPortalUrl
    }
  ]
)

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
      secrets: concat(baseSecrets, gmtSecrets, acrSecret, botApiKeySecret)
    }
    template: {
      containers: [
        {
          name: 'celery-worker'
          command: ['celery', '-A', 'app.celery_app', 'worker', '--loglevel=info', '--concurrency=1']
          image: '${acrServer}/${imageTag}'
          env: concat([
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
              name: 'DATABASE_URL'
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
              name: 'BACKEND_PLATFORM'
              value: backendPlatform
            }
          ], botApiKeyEnv, backendEnv)
          resources: {
            cpu: json('1.0')
            memory: '2.0Gi'
          }
        }
      ]
      scale: {
        minReplicas: minReplicas
        maxReplicas: maxReplicas
        rules: celeryScaleRules
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
      secrets: concat(baseSecrets, gmtSecrets, acrSecret, botApiKeySecret)
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
              name: 'DATABASE_URL'
              secretRef: 'database-url'
            }
            {
              name: 'BACKEND_PLATFORM'
              value: backendPlatform
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
