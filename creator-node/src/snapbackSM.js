const config = require('./config')
const Bull = require('bull')
const axios = require('axios')
const utils = require('./utils')
const models = require('./models')
const { logger } = require('./logging')

const DevDelayInMS = 20000
const MaxParallelSyncJobs = 10

// TODO: Discuss w/draj how to handle a long upload where the stateMachine starts processing during the operation

/*
  Snap back state machine
  Ensures file availability through sync and user replica operations
*/
class SnapbackSM {
  constructor (audiusLibs) {
    this.audiusLibs = audiusLibs
    this.initialized = false
    // Toggle to switch logs
    this.debug = true
    // Throw an error if running as creator node and no libs are provided
    if (!this.audiusLibs && !config.get('isUserMetadataNode')) {
      throw new Error('Invalid libs provided to SnapbackSM')
    }
    // State machine queue processes all user operations
    this.stateMachineQueue = this.createBullQueue('creator-node-state-machine')
    // Sync queue handles issuing sync request from primary -> secondary
    this.syncQueue = this.createBullQueue('creator-node-sync-queue')
    this.log(`Constructed snapback!`)
  }

  // Class level log output
  log (msg) {
    if (!this.debug) return
    logger.info(`SnapbackSM: ${msg}`)
  }

  /*
    Initialize queue object with provided name
  */
  createBullQueue (queueName) {
    return new Bull(
      queueName,
      {
        redis: {
          port: config.get('redisPort'),
          host: config.get('redisHost')
        }
      }
    )
  }

  // Helper function to retrieve all config based
  async getSPInfo () {
    const spID = config.get('spID')
    const endpoint = config.get('creatorNodeEndpoint')
    const delegateOwnerWallet = config.get('delegateOwnerWallet')
    const delegatePrivateKey = config.get('delegatePrivateKey')
    return {
      spID,
      endpoint,
      delegateOwnerWallet,
      delegatePrivateKey
    }
  }

  // Query eth-contracts chain for endpoint to ID info
  async recoverSpID () {
    if (config.get('spID') !== 0) {
      this.log(`Known spID=${config.get('spID')}`)
      return
    }

    const recoveredSpID = await this.audiusLibs.ethContracts.ServiceProviderFactoryClient.getServiceProviderIdFromEndpoint(
      config.get('creatorNodeEndpoint')
    )
    this.log(`Recovered ${recoveredSpID} for ${config.get('creatorNodeEndpoint')}`)
    config.set('spID', recoveredSpID)
  }

  /*
    http://audius-disc-prov_web-server_1:5000/users/creator_node?creator_node_endpoint=http://cn2_creator-node_1:4001
    Send request to discovery provider for all users with this node as primary
  */
  async getNodePrimaryUsers () {
    const currentlySelectedDiscProv = this.audiusLibs.discoveryProvider.discoveryProviderEndpoint
    let requestParams = {
      method: 'get',
      baseURL: currentlySelectedDiscProv,
      url: `users/creator_node`,
      params: {
        creator_node_endpoint: config.get('creatorNodeEndpoint') 
      }
    }
    let resp = await axios(requestParams)
    this.log(`Discovery provider: ${currentlySelectedDiscProv}`)
    // this.log(JSON.stringify(resp))
    return resp.data.data
  }

  /*
    Retrieve the current clock value on this node for the provided user wallet
  */
  async getUserPrimaryClockValue (wallet) {
    let walletPublicKey = wallet.toLowerCase()
    const cnodeUser = await models.CNodeUser.findOne({
      where: { walletPublicKey }
    })
    const clockValue = (cnodeUser) ? cnodeUser.dataValues.clock : -1
    return clockValue
  }

  /* 
    Retrieve the current clock value on a secondary node
  */
  async getSecondaryClockValue (wallet, secondaryEndpoint) {
    let resp = await axios({
      method: 'get',
      baseURL: secondaryEndpoint,
      url: `/users/clock_status/${wallet}`,
      responseType: 'json'
    })
    return resp.data.clockValue
  }

  async issueSecondarySync (userWallet, secondaryEndpoint, primaryEndpoint) {
    let syncRequestParameters = {
      baseURL: secondaryEndpoint,
      url: '/sync',
      method: 'post',
      data: {
        wallet: [userWallet],
        creator_node_endpoint: primaryEndpoint,
        // immediate: true,    // If set to true, the endpoint will not return until completed
        state_machine: true // state machine specific flag
      }
    }
    await this.syncQueue.add({ syncRequestParameters, startTime: Date.now() })
  }

  /*
    Main state machine processing function
  */
  async processStateMachineOperation (job) {
    this.log('------------------Process SnapbackSM Operation------------------')
    // TODO: Translate working branch replica set processing
    // First step here is to implement discovery provider query
    if (this.audiusLibs == null) {
      logger.error(`Invalid libs instance`)
      return
    }
    if (!this.initialized) {
      await this.initializeInternal()
      return
    }

    // 1.) Retrieve base information for state machine operations
    let spInfo = await this.getSPInfo()
    if (spInfo.spID === 0) {
      this.log(`Invalid spID, recovering ${spInfo}`)
      await this.recoverSpID()
      return
    }
    // TODO: Don't access config object every timeor abstract 
    let ownEndpoint = config.get('creatorNodeEndpoint')
    let usersList = await this.getNodePrimaryUsers()

    // Generate list of wallets to query clock number
    let nodeVectorClockQueryList = {}

    // Issue queries to secondaries for each user
    await Promise.all(
      usersList.map(
        async (user) => {
          let userWallet = user.wallet
          let secondary1 = user.secondary1
          let secondary2 = user.secondary2
          if(!nodeVectorClockQueryList[secondary1]) { nodeVectorClockQueryList[secondary1] = []}
          if(!nodeVectorClockQueryList[secondary2]) { nodeVectorClockQueryList[secondary2] = []}
          nodeVectorClockQueryList[secondary1].push(userWallet)
          nodeVectorClockQueryList[secondary2].push(userWallet)
        }
      )
    )

    // Process nodeVectorClockQueryList and cache user clock values on each node
    let secondaryNodesToProcess = Object.keys(nodeVectorClockQueryList)
    let secondaryNodeUserClockStatus = {}
    await Promise.all(
      secondaryNodesToProcess.map(
        async (node) => {
          secondaryNodeUserClockStatus[node] = {}
          // TODO: Batch this too?
          let walletsToQuery = nodeVectorClockQueryList[node]
          let requestParams = {
            baseURL: node,
            method: 'post',
            url: '/users/batch_clock_status',
            data: {
              "walletPublicKeys": walletsToQuery
            }
          }
          let resp = await axios(requestParams)
          let userClockStatusList = resp.data.users
          // Process returned clock values from this secondary node
          userClockStatusList.map((entry) => {
            secondaryNodeUserClockStatus[node][entry.walletPublicKey] = entry.clock
          })
        }
      )
    )

    // Issue syncs if necessary
    // For each user in the initially returned usersList,
    //  compare local primary clock value to value from secondary retrieved in bulk above
    await Promise.all(usersList.map(
      async (user) => {
        let userWallet = user.wallet
        let secondary1 = user.secondary1
        let secondary2 = user.secondary2
        let primaryClockValue = await this.getUserPrimaryClockValue(userWallet)
        let secondary1ClockValue = secondaryNodeUserClockStatus[secondary1][userWallet]
        let secondary2ClockValue = secondaryNodeUserClockStatus[secondary2][userWallet]
        let secondary1SyncRequired = secondary1ClockValue === undefined ? true : primaryClockValue > secondary1ClockValue
        let secondary2SyncRequired = secondary2ClockValue === undefined ? true : primaryClockValue > secondary2ClockValue
        this.log(`${userWallet} primaryClock=${primaryClockValue}, (secondary1=${secondary1}, clock=${secondary1ClockValue} syncRequired=${secondary1SyncRequired}), (secondary2=${secondary2}, clock=${secondary2ClockValue}, syncRequired=${secondary2SyncRequired})`)
        // Enqueue sync for secondary1 if required
        if (secondary1SyncRequired) {
          // Issue sync
          await this.issueSecondarySync(userWallet, secondary1, ownEndpoint)
        }
        // Enqueue sync for secondary2 if required
        if (secondary2SyncRequired) {
          // Issue sync
          await this.issueSecondarySync(userWallet, secondary2, ownEndpoint)
        }
      }
    ))
    this.log('------------------END Process SnapbackSM Operation------------------')
  }

  // Main sync queue job
  async processSyncOperation(job) {
    const syncRequestParameters = job.data.syncRequestParameters
    const syncWallet = syncRequestParameters.data.wallet[0]
    const primaryClockValue = await this.getUserPrimaryClockValue(syncWallet)
    const secondaryUrl = syncRequestParameters.baseURL
    this.log(`------------------Process SYNC | User ${syncWallet} | Target: ${secondaryUrl} ------------------`)
    // TODO: Consider a short-circuit here if sync is already in progress? Or is it worth issuing anyway
    // TODO: Expand this and actually check validity of data params
    let isValidSyncJobData = (
      ('baseURL' in syncRequestParameters) &&
      ('url' in syncRequestParameters) &&
      ('method' in syncRequestParameters) &&
      ('data' in syncRequestParameters)
    )
    if (!isValidSyncJobData) {
      logger.error(`Invalid sync data found`)
      logger.error(job.data)
      return
    }
    // Issue sync request to secondary
    await axios(syncRequestParameters)

    // Monitor the sync status
    let syncMonitoringRequestParameters = {
      method: 'get',
      baseURL: secondaryUrl,
      url: `/sync_status/${syncWallet}`,
      responseType: 'json'
    }
    // sync_status is expected to fail during an ongoing sync operation, monitor until success or timeout
    let syncAttemptCompleted = false
    // 1minute in ms 
    let maxSyncMonitoringDurationInMs = 10000
    let startTime = Date.now()
    while (!syncAttemptCompleted) {
      try {
        let syncMonitoringResp = await axios(syncMonitoringRequestParameters)
        let respData = syncMonitoringResp.data.data
        this.log(`processSync ${syncWallet} secondary response: ${JSON.stringify(respData)}`)
        if (respData.clockValue === primaryClockValue) {
          syncAttemptCompleted = true
          this.log(`processSync ${syncWallet} clockValue from secondary:${respData.clockValue}, primary:${primaryClockValue}`)
        }
      } catch(e) {
        this.log(`processSync ${syncWallet} error querying sync_status: ${e}`)
      }
      if (Date.now() - startTime > maxSyncMonitoringDurationInMs) {
        this.log(`ERROR: processSync ${syncWallet} timeout for ${syncWallet}`)
        syncAttemptCompleted = true
      }
      // 1s delay between retries
      await utils.timeout(1000)
    }
    // Exit when sync status is computed
    // Determine how many times to retry this operation
    this.log('------------------END Process SYNC------------------')
  }

  async initializeInternal () {
    const endpoint = config.get('creatorNodeEndpoint')
    this.log(`Initializing SnapbackSM`)
    this.log(`Retrieving spID for ${endpoint}`)
    const recoveredSpID = await this.audiusLibs.ethContracts.ServiceProviderFactoryClient.getServiceProviderIdFromEndpoint(
      endpoint
    )
    // A returned spID of 0 means this endpoint is currently not registered on chain
    // In this case, the stateMachine is considered to be 'uninitialized'
    if (recoveredSpID === 0) {
      this.initialized = false
    }
    config.set('spID', recoveredSpID)
    this.log(`Recovered ${config.get('spID')} for ${endpoint}`)
    this.initialized = true
    return this.initialized
  }

  /*
    Initialize the configs necessary to run
  */
  async init () {
    await this.stateMachineQueue.empty()
    await this.syncQueue.empty()

    const isUserMetadata = config.get('isUserMetadataNode')
    if (isUserMetadata) {
      this.log(`SnapbackSM disabled for userMetadataNode. ${endpoint}, isUserMetadata=${isUserMetadata}`)
      return
    }

    await this.initializeInternal()
    if (!this.initialized) {
      return
    }


    // TODO: Enable after dev
    // Run the task every x time interval
    // this.stateMachineQueue.add({}, { repeat: { cron: '0 */x * * *' } })

    // Enqueue first state machine operation
    // TODO: Remove this line permanently prior to final check-in
    this.stateMachineQueue.add({ startTime: Date.now() })

    // Process state machine operations
    this.stateMachineQueue.process(
      async (job, done) => {
        try {
          await this.processStateMachineOperation(job)
        } catch (e) {
          this.log(`stateMachineQueue error processing ${e}`)
        } finally {
          // TODO: Remove dev mode
          this.log(`DEV MODE next job in ${DevDelayInMS}ms at ${new Date(Date.now() + DevDelayInMS)}`)
          await utils.timeout(DevDelayInMS)
          this.stateMachineQueue.add({ startTime: Date.now() })
          done()
        }
      }
    )

    // Entrypoint to the sync queue, as drained will issue syncs
    this.syncQueue.process(
      MaxParallelSyncJobs,
      async (job, done) => {
        try {
          await this.processSyncOperation(job)
        } catch (e) {
          this.log(`syncQueue error processing ${e}`)
        } finally {
          // Restart job
          // Can be replaced with cron after development is complete
          done()
        }
      }
    )
  }
}

module.exports = SnapbackSM
