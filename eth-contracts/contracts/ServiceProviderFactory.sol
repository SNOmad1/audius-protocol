pragma solidity ^0.5.0;

import "@openzeppelin/contracts-ethereum-package/contracts/math/SafeMath.sol";
import "./ServiceTypeManager.sol";
import "./ClaimsManager.sol";
import "./Staking.sol";


contract ServiceProviderFactory is InitializableV2 {
    using SafeMath for uint;

    address private stakingAddress;
    address private delegateManagerAddress;
    address private governanceAddress;
    address private serviceTypeManagerAddress;
    address private claimsManagerAddress;
    uint private decreaseStakeLockupDuration;

    /// @dev - Stores following entities
    ///        1) Directly staked amount by SP, not including delegators
    ///        2) % Cut of delegator tokens taken during reward
    ///        3) Bool indicating whether this SP has met min/max requirements
    ///        4) Number of endpoints registered by SP
    ///        5) Minimum total stake for this account
    ///        6) Maximum total stake for this account
    struct ServiceProviderDetails {
        uint deployerStake;
        uint deployerCut;
        bool validBounds;
        uint numberOfEndpoints;
        uint minAccountStake;
        uint maxAccountStake;
    }

    /// @dev - Data structure for time delay during withdrawal
    struct DecreaseStakeRequest {
        uint decreaseAmount;
        uint lockupExpiryBlock;
    }

    /// @dev - Mapping of service provider address to details
    mapping(address => ServiceProviderDetails) spDetails;

    /// @dev - Minimum staked by service provider account deployer
    /// @dev - Static regardless of total number of endpoints for a given account
    uint minDeployerStake;

    /// @dev - standard - imitates relationship between Ether and Wei
    uint8 private constant DECIMALS = 18;

    /// @dev - denominator for deployer cut calculations
    /// @dev - user values are intended to be x/DEPLOYER_CUT_BASE
    uint private constant DEPLOYER_CUT_BASE = 100;

    /// @dev - Struct maintaining information about sp
    struct ServiceEndpoint {
        address owner;
        string endpoint;
        uint blocknumber;
        address delegateOwnerWallet;
    }

    /// @dev - Uniquely assigned serviceProvider ID, incremented for each service type
    /// @notice - Keeps track of the total number of services registered regardless of
    ///           whether some have been deregistered since
    mapping(bytes32 => uint) serviceProviderTypeIDs;

    /// @dev - mapping of (serviceType -> (serviceInstanceId <-> serviceProviderInfo))
    /// @notice - stores the actual service provider data like endpoint and owner wallet
    ///           with the ability lookup by service type and service id */
    mapping(bytes32 => mapping(uint => ServiceEndpoint)) serviceProviderInfo;

    /// @dev - mapping of keccak256(endpoint) to uint ID
    /// @notice - used to check if a endpoint has already been registered and also lookup
    /// the id of an endpoint
    mapping(bytes32 => uint) serviceProviderEndpointToId;

    /// @dev - mapping of address -> sp id array */
    /// @notice - stores all the services registered by a provider. for each address,
    /// provides the ability to lookup by service type and see all registered services
    mapping(address => mapping(bytes32 => uint[])) serviceProviderAddressToId;

    /// @dev - Mapping of service provider -> decrease stake request
    mapping(address => DecreaseStakeRequest) decreaseStakeRequests;

    event RegisteredServiceProvider(
      uint _spID,
      bytes32 _serviceType,
      address _owner,
      string _endpoint,
      uint256 _stakeAmount
    );

    event DeregisteredServiceProvider(
      uint _spID,
      bytes32 _serviceType,
      address _owner,
      string _endpoint,
      uint256 _unstakeAmount
    );

    event UpdatedStakeAmount(
      address _owner,
      uint256 _stakeAmount
    );

    event UpdateEndpoint(
      bytes32 _serviceType,
      address _owner,
      string _oldEndpoint,
      string _newEndpoint,
      uint spId
    );

    function initialize (
        address _registryAddress,
        address _governanceAddress
    ) public initializer
    {
        require(
            _registryAddress != address(0x00),
            "Requires non-zero _registryAddress"
        );
        governanceAddress = _governanceAddress;

        // Configure direct minimum stake for deployer
        minDeployerStake = 5 * 10**uint256(DECIMALS);

        // 10 blocks for lockup duration
        decreaseStakeLockupDuration = 10;

        InitializableV2.initialize();
    }

    /// @notice - Register a new endpoint to the account of msg.sender
    ///           Transfers stake into staking pool
    function register(
        bytes32 _serviceType,
        string calldata _endpoint,
        uint256 _stakeAmount,
        address _delegateOwnerWallet
    ) external returns (uint spID)
    {
        _requireIsInitialized();
        require(serviceTypeManagerAddress != address(0x00), "serviceTypeManagerAddress not set");
        require(stakingAddress != address(0x00), "stakingAddress not set");

        require(
            ServiceTypeManager(serviceTypeManagerAddress).serviceTypeIsValid(_serviceType),
            "Valid service type required");

        // Stake token amount from msg.sender
        if (_stakeAmount > 0) {
            require(!_claimPending(msg.sender), "No claim expected to be pending prior to stake transfer");
            Staking(stakingAddress).stakeFor(msg.sender, _stakeAmount);
        }

        require (
            serviceProviderEndpointToId[keccak256(bytes(_endpoint))] == 0,
            "Endpoint already registered");

        uint newServiceProviderID = serviceProviderTypeIDs[_serviceType].add(1);
        serviceProviderTypeIDs[_serviceType] = newServiceProviderID;

        // Index spInfo
        serviceProviderInfo[_serviceType][newServiceProviderID] = ServiceEndpoint({
            owner: msg.sender,
            endpoint: _endpoint,
            blocknumber: block.number,
            delegateOwnerWallet: _delegateOwnerWallet
        });

        // Update endpoint mapping
        serviceProviderEndpointToId[keccak256(bytes(_endpoint))] = newServiceProviderID;

        // Update (address -> type -> ids[])
        serviceProviderAddressToId[msg.sender][_serviceType].push(newServiceProviderID);

        // Increment number of endpoints for this address
        spDetails[msg.sender].numberOfEndpoints = spDetails[msg.sender].numberOfEndpoints.add(1);

        // Update deployer total
        spDetails[msg.sender].deployerStake = (
            spDetails[msg.sender].deployerStake.add(_stakeAmount)
        );

        // Update min and max totals for this service provider
        (uint typeMin, uint typeMax) = ServiceTypeManager(
            serviceTypeManagerAddress
        ).getServiceTypeStakeInfo(_serviceType);
        spDetails[msg.sender].minAccountStake = spDetails[msg.sender].minAccountStake.add(typeMin);
        spDetails[msg.sender].maxAccountStake = spDetails[msg.sender].maxAccountStake.add(typeMax);

        // Confirm both aggregate account balance and directly staked amount are valid
        uint currentlyStakedForOwner = this.validateAccountStakeBalance(msg.sender);

        // Indicate this service provider is within bounds
        spDetails[msg.sender].validBounds = true;

        emit RegisteredServiceProvider(
            newServiceProviderID,
            _serviceType,
            msg.sender,
            _endpoint,
            currentlyStakedForOwner
        );

        return newServiceProviderID;
    }

    /// @notice - Deregister an endpoint from the account of msg.sender
    ///           Removes stake if this is the final endpoint remaining for account
    function deregister(
        bytes32 _serviceType,
        string calldata _endpoint
    ) external returns (uint deregisteredSpID)
    {
        _requireIsInitialized();

        // Unstake on deregistration if and only if this is the last service endpoint
        uint unstakeAmount = 0;
        bool unstaked = false;
        // owned by the service provider
        if (spDetails[msg.sender].numberOfEndpoints == 1) {
            unstakeAmount = spDetails[msg.sender].deployerStake;

            // Submit request to decrease stake, overriding any pending request
            decreaseStakeRequests[msg.sender] = DecreaseStakeRequest({
                decreaseAmount: unstakeAmount,
                lockupExpiryBlock: block.number.add(decreaseStakeLockupDuration)
            });

            unstaked = true;
        }

        require (
            serviceProviderEndpointToId[keccak256(bytes(_endpoint))] != 0,
            "Endpoint not registered");

        // Cache invalided service provider ID
        uint deregisteredID = serviceProviderEndpointToId[keccak256(bytes(_endpoint))];

        // Update endpoint mapping
        serviceProviderEndpointToId[keccak256(bytes(_endpoint))] = 0;

        require(
            keccak256(bytes(serviceProviderInfo[_serviceType][deregisteredID].endpoint)) == keccak256(bytes(_endpoint)),
            "Invalid endpoint for service type");

        require (
            serviceProviderInfo[_serviceType][deregisteredID].owner == msg.sender,
            "Only callable by endpoint owner");

        // Update info mapping
        delete serviceProviderInfo[_serviceType][deregisteredID];
        // Reset id, update array
        uint spTypeLength = serviceProviderAddressToId[msg.sender][_serviceType].length;
        for (uint i = 0; i < spTypeLength; i ++) {
            if (serviceProviderAddressToId[msg.sender][_serviceType][i] == deregisteredID) {
                // Overwrite element to be deleted with last element in array
                serviceProviderAddressToId[msg.sender][_serviceType][i] = serviceProviderAddressToId[msg.sender][_serviceType][spTypeLength - 1];
                // Reduce array size, exit loop
                serviceProviderAddressToId[msg.sender][_serviceType].length--;
                // Confirm this ID has been found for the service provider
                break;
            }
        }

        // Decrement number of endpoints for this address
        spDetails[msg.sender].numberOfEndpoints -= 1;

        // Update min and max totals for this service provider
        (uint typeMin, uint typeMax) = ServiceTypeManager(
            serviceTypeManagerAddress
        ).getServiceTypeStakeInfo(_serviceType);
        spDetails[msg.sender].minAccountStake = spDetails[msg.sender].minAccountStake.sub(typeMin);
        spDetails[msg.sender].maxAccountStake = spDetails[msg.sender].maxAccountStake.sub(typeMax);

        emit DeregisteredServiceProvider(
            deregisteredID,
            _serviceType,
            msg.sender,
            _endpoint,
            unstakeAmount);

        // Confirm both aggregate account balance and directly staked amount are valid
        // Only if unstake operation has not occurred
        if (!unstaked) {
            this.validateAccountStakeBalance(msg.sender);
            // Indicate this service provider is within bounds
            spDetails[msg.sender].validBounds = true;
        }

        return deregisteredID;
    }

    function increaseStake(
        uint256 _increaseStakeAmount
    ) external returns (uint newTotalStake)
    {
        _requireIsInitialized();

        // Confirm owner has an endpoint
        require(
            spDetails[msg.sender].numberOfEndpoints > 0,
            "Registered endpoint required to increase stake"
        );
        require(
            !_claimPending(msg.sender),
            "No claim expected to be pending prior to stake transfer"
        );

        Staking stakingContract = Staking(
            stakingAddress
        );

        // Stake increased token amount for msg.sender
        stakingContract.stakeFor(msg.sender, _increaseStakeAmount);

        uint newStakeAmount = stakingContract.totalStakedFor(msg.sender);

        // Update deployer total
        spDetails[msg.sender].deployerStake = (
            spDetails[msg.sender].deployerStake.add(_increaseStakeAmount)
        );

        // Confirm both aggregate account balance and directly staked amount are valid
        this.validateAccountStakeBalance(msg.sender);

        // Indicate this service provider is within bounds
        spDetails[msg.sender].validBounds = true;

        emit UpdatedStakeAmount(
            msg.sender,
            newStakeAmount
        );

        return newStakeAmount;
    }

    function requestDecreaseStake(uint _decreaseStakeAmount)
    external returns (uint newStakeAmount)
    {
        _requireIsInitialized();
        require(
            !_claimPending(msg.sender),
            "No claim expected to be pending prior to stake transfer"
        );

        Staking stakingContract = Staking(
            stakingAddress
        );

        uint currentStakeAmount = stakingContract.totalStakedFor(msg.sender);

        // Prohibit decreasing stake to invalid bounds
        _validateBalanceInternal(msg.sender, (currentStakeAmount.sub(_decreaseStakeAmount)));

        decreaseStakeRequests[msg.sender] = DecreaseStakeRequest({
            decreaseAmount: _decreaseStakeAmount,
            lockupExpiryBlock: block.number.add(decreaseStakeLockupDuration)
        });

        return currentStakeAmount.sub(_decreaseStakeAmount);
    }

    function cancelDecreaseStakeRequest(address _account) external
    {
        require(
            msg.sender == _account || msg.sender == delegateManagerAddress,
            "Only callable from owner or DelegateManager"
        );
        require(_decreaseRequestIsPending(_account), "Decrease stake request must be pending");

        // Clear decrease stake request
        decreaseStakeRequests[_account] = DecreaseStakeRequest({
            decreaseAmount: 0,
            lockupExpiryBlock: 0
        });
    }

    function decreaseStake() external returns (uint newTotalStake)
    {
        _requireIsInitialized();

        require(_decreaseRequestIsPending(msg.sender), "Decrease stake request must be pending");
        require(
            decreaseStakeRequests[msg.sender].lockupExpiryBlock <= block.number,
            "Lockup must be expired"
        );

        Staking stakingContract = Staking(
            stakingAddress
        );

        // Decrease staked token amount for msg.sender
        stakingContract.unstakeFor(msg.sender, decreaseStakeRequests[msg.sender].decreaseAmount);

        // Query current stake
        uint newStakeAmount = stakingContract.totalStakedFor(msg.sender);

        // Update deployer total
        spDetails[msg.sender].deployerStake = (
            spDetails[msg.sender].deployerStake.sub(decreaseStakeRequests[msg.sender].decreaseAmount)
        );

        // Confirm both aggregate account balance and directly staked amount are valid
        // During registration this validation is bypassed since no endpoints remain
        if (spDetails[msg.sender].numberOfEndpoints > 0) {
            this.validateAccountStakeBalance(msg.sender);
        }

        // Indicate this service provider is within bounds
        spDetails[msg.sender].validBounds = true;

        // Clear decrease stake request
        delete decreaseStakeRequests[msg.sender];

        emit UpdatedStakeAmount(
            msg.sender,
            newStakeAmount
        );

        return newStakeAmount;
    }

    function updateDelegateOwnerWallet(
        bytes32 _serviceType,
        string calldata _endpoint,
        address _updatedDelegateOwnerWallet
    ) external
    {
        uint spID = this.getServiceProviderIdFromEndpoint(_endpoint);

        require(
            serviceProviderInfo[_serviceType][spID].owner == msg.sender,
            "Invalid update operation, wrong owner");

        serviceProviderInfo[_serviceType][spID].delegateOwnerWallet = _updatedDelegateOwnerWallet;
    }

    function updateEndpoint(
        bytes32 _serviceType,
        string calldata _oldEndpoint,
        string calldata _newEndpoint
    ) external returns (uint spID)
    {
        uint spId = this.getServiceProviderIdFromEndpoint(_oldEndpoint);

        require (spId != 0, "Could not find service provider with that endpoint");

        ServiceEndpoint memory sp = serviceProviderInfo[_serviceType][spId];

        require(sp.owner == msg.sender,"Invalid update endpoint operation, wrong owner");

        require(
            keccak256(bytes(sp.endpoint)) == keccak256(bytes(_oldEndpoint)),
            "Old endpoint doesn't match what's registered for the service provider"
        );

        // invalidate old endpoint
        serviceProviderEndpointToId[keccak256(bytes(sp.endpoint))] = 0;

        // update to new endpoint
        sp.endpoint = _newEndpoint;
        serviceProviderInfo[_serviceType][spId] = sp;
        serviceProviderEndpointToId[keccak256(bytes(_newEndpoint))] = spId;
        return spId;
    }

    /// @notice Update service provider balance
    function updateServiceProviderStake(
        address _serviceProvider,
        uint _amount
     ) external
    {
        require(delegateManagerAddress != address(0x00), "delegateManagerAddress not set");
        require(
            msg.sender == delegateManagerAddress,
            "updateServiceProviderStake - only callable by DelegateManager"
        );
        // Update SP tracked total
        spDetails[_serviceProvider].deployerStake = _amount;
        _updateServiceProviderBoundStatus(_serviceProvider);
    }

    /// @notice Update service provider cut
    /// SPs will interact with this value as a percent, value translation done client side
    function updateServiceProviderCut(
        address _serviceProvider,
        uint _cut
    ) external
    {
        require(
            msg.sender == _serviceProvider,
            "Service Provider cut update operation restricted to deployer");

        require(
            _cut <= DEPLOYER_CUT_BASE,
            "Service Provider cut cannot exceed base value");
        spDetails[_serviceProvider].deployerCut = _cut;
    }

    /// @notice Update service provider lockup duration
    function updateDecreaseStakeLockupDuration(uint _duration) external {
        _requireIsInitialized();

        require(
            msg.sender == governanceAddress,
            "Only callable by Governance contract"
        );

        decreaseStakeLockupDuration = _duration;
    }

    /// @notice Denominator for deployer cut calculations
    function getServiceProviderDeployerCutBase()
    external pure returns (uint base)
    {
        return DEPLOYER_CUT_BASE;
    }

    function getTotalServiceTypeProviders(bytes32 _serviceType)
    external view returns (uint numberOfProviders)
    {
        return serviceProviderTypeIDs[_serviceType];
    }

    function getServiceProviderIdFromEndpoint(string calldata _endpoint)
    external view returns (uint spID)
    {
        return serviceProviderEndpointToId[keccak256(bytes(_endpoint))];
    }

    function getMinDeployerStake()
    external view returns (uint min)
    {
        return minDeployerStake;
    }

    function getServiceProviderIdsFromAddress(address _ownerAddress, bytes32 _serviceType)
    external view returns (uint[] memory spIds)
    {
        return serviceProviderAddressToId[_ownerAddress][_serviceType];
    }

    function getServiceEndpointInfo(bytes32 _serviceType, uint _serviceId)
    external view returns (address owner, string memory endpoint, uint blockNumber, address delegateOwnerWallet)
    {
        ServiceEndpoint memory sp = serviceProviderInfo[_serviceType][_serviceId];
        return (sp.owner, sp.endpoint, sp.blocknumber, sp.delegateOwnerWallet);
    }

    function getServiceProviderDetails(address _sp)
    external view returns (
        uint deployerStake,
        uint deployerCut,
        bool validBounds,
        uint numberOfEndpoints,
        uint minAccountStake,
        uint maxAccountStake)
    {
        return (
            spDetails[_sp].deployerStake,
            spDetails[_sp].deployerCut,
            spDetails[_sp].validBounds,
            spDetails[_sp].numberOfEndpoints,
            spDetails[_sp].minAccountStake,
            spDetails[_sp].maxAccountStake
        );
    }

    /// @notice Pending decrease stake request
    function getPendingDecreaseStakeRequest(address _sp)
    external view returns (uint amount, uint lockupExpiryBlock)
    {
        return (
            decreaseStakeRequests[_sp].decreaseAmount,
            decreaseStakeRequests[_sp].lockupExpiryBlock
        );
    }

    /// @notice Current unstake lockup duration
    function getDecreaseStakeLockupDuration()
    external view returns (uint duration)
    {
        return decreaseStakeLockupDuration;
    }

    /// @notice Validate that the total service provider balance is between the min and max stakes for all their registered services
    //          Validates that direct stake for sp is also above minimum
    function validateAccountStakeBalance(address _sp)
    external view returns (uint stakedForOwner)
    {
        uint currentlyStakedForOwner = Staking(stakingAddress).totalStakedFor(_sp);
        _validateBalanceInternal(_sp, currentlyStakedForOwner);
        return currentlyStakedForOwner;
    }

    function getGovernanceAddress() external view returns (address addr) {
        return governanceAddress;
    }

    function getStakingAddress() external view returns (address addr) {
        return stakingAddress;
    }

    function getDelegateManagerAddress() external view returns (address addr) {
        return delegateManagerAddress;
    }

    function getServiceTypeManagerAddress() external view returns (address addr) {
        return serviceTypeManagerAddress;
    }

    function getClaimsManagerAddress() external view returns (address addr) {
        return claimsManagerAddress;
    }

    function setGovernanceAddress(address _address) external {
        require(msg.sender == governanceAddress, "Only callable by Governance contract");
        governanceAddress = _address;
    }

    function setStakingAddress(address _address) external {
        require(msg.sender == governanceAddress, "Only callable by Governance contract");
        stakingAddress = _address;
    }

    function setDelegateManagerAddress(address _address) external {
        require(msg.sender == governanceAddress, "Only callable by Governance contract");
        delegateManagerAddress = _address;
    }

    function setServiceTypeManagerAddress(address _address) external {
        require(msg.sender == governanceAddress, "Only callable by Governance contract");
        serviceTypeManagerAddress = _address;
    }

    function setClaimsManagerAddress(address _address) external {
        require(msg.sender == governanceAddress, "Only callable by Governance contract");
        claimsManagerAddress = _address;
    }

    /**
     * @notice Update service provider bound status
     */
    function _updateServiceProviderBoundStatus(address _serviceProvider) internal {
        // Validate bounds for total stake
        uint totalSPStake = Staking(stakingAddress).totalStakedFor(_serviceProvider);
        if (totalSPStake < spDetails[_serviceProvider].minAccountStake ||
            totalSPStake > spDetails[_serviceProvider].maxAccountStake) {
            // Indicate this service provider is out of bounds
            spDetails[_serviceProvider].validBounds = false;
        } else {
            // Indicate this service provider is within bounds
            spDetails[_serviceProvider].validBounds = true;
        }
    }

    /**
     * @notice Compare a given service provider bounds to input amount
     */
    function _validateBalanceInternal(address _sp, uint _amount) internal view
    {
        require(
            _amount >= spDetails[_sp].minAccountStake,
            "Minimum stake threshold exceeded");

        require(
            _amount <= spDetails[_sp].maxAccountStake,
            "Maximum stake amount exceeded");

        require(
            spDetails[_sp].deployerStake == 0 || spDetails[_sp].deployerStake >= minDeployerStake,
            "Direct stake restriction violated for this service provider");
    }

    /**
     * @notice Boolean indicating whether a decrease request has been initiated
     */
    function _decreaseRequestIsPending(address _serviceProvider)
    internal view returns (bool pending)
    {
        return (
            (decreaseStakeRequests[_serviceProvider].lockupExpiryBlock > 0) &&
            (decreaseStakeRequests[_serviceProvider].decreaseAmount > 0)
        );
    }

    /**
     * @notice Boolean indicating whether a claim is pending for this service provider
     */
    function _claimPending(address _sp) internal view returns (bool pending) {
        return ClaimsManager(claimsManagerAddress).claimPending(_sp);
    }
}
