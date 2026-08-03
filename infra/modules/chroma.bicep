@description('Location for all resources')
param location string

@description('Environment name (dev or prod)')
param environment string

@description('Container Apps Environment ID')
param environmentId string

var appName = 'chromadb-${environment}'

resource chromaApp 'Microsoft.App/containerApps@2023-05-01' = {
  name: appName
  location: location
  properties: {
    managedEnvironmentId: environmentId
    configuration: {
      activeRevisionsMode: 'Single'
      ingress: {
        external: false
        targetPort: 8000
        transport: 'auto'
      }
    }
    template: {
      containers: [
        {
          name: 'chromadb'
          image: 'chromadb/chroma:1.5.9'
          env: [
            {
              name: 'CHROMA_SERVER_HOST'
              value: '0.0.0.0'
            }
            {
              name: 'IS_PERSISTENT'
              value: 'TRUE'
            }
            {
              name: 'ANONYMIZED_TELEMETRY'
              value: 'FALSE'
            }
          ]
          volumeMounts: [
            {
              volumeName: 'chroma-data-vol'
              mountPath: '/chroma/chroma'
            }
          ]
          resources: {
            cpu: json('1.0')
            memory: '2.0Gi'
          }
        }
      ]
      volumes: [
        {
          name: 'chroma-data-vol'
          storageName: 'chroma-storage'
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

output chromaHost string = appName
output chromaPort int = 8000
