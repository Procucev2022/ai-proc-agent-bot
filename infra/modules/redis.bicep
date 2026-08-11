@description('Location for all resources')
param location string

@description('Environment name (dev or prod)')
param environment string

@description('Container Apps Environment ID')
param environmentId string

var appName = 'redis-${environment}'

resource redisApp 'Microsoft.App/containerApps@2023-05-01' = {
  name: appName
  location: location
  properties: {
    managedEnvironmentId: environmentId
    configuration: {
      activeRevisionsMode: 'Single'
      ingress: {
        external: false
        targetPort: 6379
        transport: 'tcp'
      }
    }
    template: {
      containers: [
        {
          name: 'redis'
          image: 'redis:7-alpine'
          command: [
            'redis-server'
            '--appendonly'
            'yes'
            '--maxmemory'
            '1gb'
            '--maxmemory-policy'
            'allkeys-lru'
          ]
          volumeMounts: [
            {
              volumeName: 'redis-data-vol'
              mountPath: '/data'
            }
          ]
          resources: {
            cpu: json('0.5')
            memory: '1.0Gi'
          }
        }
      ]
      volumes: [
        {
          name: 'redis-data-vol'
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
}

output redisHost string = appName
output redisPort int = 6379
output redisUrl string = 'redis://${appName}:6379/0'
output celeryBrokerUrl string = 'redis://${appName}:6379/1'
