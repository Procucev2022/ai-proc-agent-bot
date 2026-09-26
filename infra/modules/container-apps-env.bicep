@description('Location for all resources')
param location string

@description('Environment name (dev or prod)')
param environment string

@description('Base name for resources')
param baseName string

var logAnalyticsName = 'law-${baseName}-${environment}'
var uniqueSuffix = substring(uniqueString(resourceGroup().id), 0, 8)
var storageAccountName = 'st${replace(baseName, '-', '')}${environment}${uniqueSuffix}'
var redisFileShareName = 'redis-data'
var envName = 'cae-${baseName}-${environment}'

// Log Analytics Workspace
resource logAnalytics 'Microsoft.OperationalInsights/workspaces@2022-10-01' = {
  name: logAnalyticsName
  location: location
  properties: {
    sku: {
      name: 'PerGB2018'
    }
    retentionInDays: 30
  }
}

// Storage Account for Persistent Volume (Redis)
resource storageAccount 'Microsoft.Storage/storageAccounts@2023-01-01' = {
  name: storageAccountName
  location: location
  sku: {
    name: 'Standard_LRS'
  }
  kind: 'StorageV2'
  properties: {
    accessTier: 'Hot'
  }
}

// File Services
resource fileServices 'Microsoft.Storage/storageAccounts/fileServices@2023-01-01' = {
  parent: storageAccount
  name: 'default'
}

// Azure File Share for Redis persistence (AOF)
resource redisFileShare 'Microsoft.Storage/storageAccounts/fileServices/shares@2023-01-01' = {
  parent: fileServices
  name: redisFileShareName
  properties: {
    shareQuota: 8
  }
}

// Azure Container Apps Managed Environment
resource containerAppsEnv 'Microsoft.App/managedEnvironments@2023-05-01' = {
  name: envName
  location: location
  properties: {
    appLogsConfiguration: {
      destination: 'log-analytics'
      logAnalyticsConfiguration: {
        customerId: logAnalytics.properties.customerId
        sharedKey: logAnalytics.listKeys().primarySharedKey
      }
    }
  }
}

// Attach Redis file share to the Container Apps Environment.
// redis.bicep mounts this by the name 'redis-storage'.
resource redisEnvStorage 'Microsoft.App/managedEnvironments/storages@2023-05-01' = {
  parent: containerAppsEnv
  name: 'redis-storage'
  properties: {
    azureFile: {
      accountName: storageAccount.name
      accountKey: storageAccount.listKeys().keys[0].value
      shareName: redisFileShareName
      accessMode: 'ReadWrite'
    }
  }
  dependsOn: [
    redisFileShare
  ]
}

output environmentId string = containerAppsEnv.id
output environmentName string = containerAppsEnv.name
output logAnalyticsWorkspaceId string = logAnalytics.id
output storageAccountName string = storageAccount.name
