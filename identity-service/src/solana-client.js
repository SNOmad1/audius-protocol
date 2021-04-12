const config = require('./config')
const solanaWeb3 = require('@solana/web3.js')
const keccak256 = require('keccak256')
const secp256k1 = require('secp256k1')
const borsh = require('borsh')

const VALID_SIGNER = config.get('solanaValidSigner')
const AUDIUS_PROGRAM = new solanaWeb3.PublicKey(
  config.get('solanaProgramAddress')
)
const CREATE_AND_VERIFY_PROGRAM = new solanaWeb3.PublicKey(
  config.get('solanaCreateAndVerifyAddress')
)
const INSTRUCTIONS_PROGRAM = new solanaWeb3.PublicKey(
  'Sysvar1nstructions1111111111111111111111111'
)

const feePayer = new solanaWeb3.Account(config.get('solanaFeePayerWallet'))

class Assignable {
  constructor (data) {
    Object.assign(this, data)
  }
}

class TrackData extends Assignable {}
class InstructionArgs extends Assignable {}
class InstructionEnum extends Assignable {}

const trackDataSchema = new Map([
  [
    TrackData,
    {
      kind: 'struct',
      fields: [
        ['user_id', 'string'],
        ['track_id', 'string'],
        ['source', 'string']
      ]
    }
  ]
])

const instructionSchema = new Map([
  [
    InstructionEnum,
    {
      kind: 'enum',
      field: 'choose',
      values: [['instruction', InstructionArgs]]
    }
  ],
  [
    InstructionArgs,
    {
      kind: 'struct',
      fields: [
        ['track_data', TrackData],
        ['signature', [64]],
        ['recovery_id', 'u8']
      ]
    }
  ],
  [
    TrackData,
    {
      kind: 'struct',
      fields: [
        ['user_id', 'string'],
        ['track_id', 'string'],
        ['source', 'string']
      ]
    }
  ]
])

let devnetConnection = new solanaWeb3.Connection(config.get('solanaEndpoint'))

async function createAndVerifyMessage (
  validSigner,
  privateKey,
  userId,
  trackId,
  source
) {
  validSigner = validSigner || VALID_SIGNER

  let privKey = Buffer.from(privateKey, 'hex')
  let pubKey = secp256k1.publicKeyCreate(privKey, false).slice(1)

  let validSignerPubK = new solanaWeb3.PublicKey(validSigner)
  let accInfo = await devnetConnection.getAccountInfo(validSignerPubK)
  let signerGroup = new solanaWeb3.PublicKey(
    accInfo.data.toJSON().data.slice(1, 33)
  ) // cut off version and eth address from valid signer data

  let trackData = new TrackData({
    user_id: userId,
    track_id: trackId,
    source: source
  })

  const serializedTrackData = borsh.serialize(trackDataSchema, trackData)
  let msgHash = keccak256(serializedTrackData.toJSON().data)

  const sigObj = secp256k1.ecdsaSign(Uint8Array.from(msgHash), privKey)

  let instructionArgs = new InstructionArgs({
    track_data: trackData,
    signature: Array.from(sigObj.signature),
    recovery_id: sigObj.recid
  })

  let instructionData = new InstructionEnum({
    instruction: instructionArgs,
    choose: 'instruction'
  })

  const serializedInstructionArgs = borsh.serialize(
    instructionSchema,
    instructionData
  )

  let transaction = new solanaWeb3.Transaction()

  let secpInstruction = solanaWeb3.Secp256k1Program.createInstructionWithPublicKey(
    {
      publicKey: pubKey,
      message: serializedTrackData.toJSON().data,
      signature: sigObj.signature,
      recoveryId: sigObj.recid
    }
  )

  transaction.add(secpInstruction)

  transaction.add({
    keys: [
      { pubkey: validSignerPubK, isSigner: false, isWritable: false },
      { pubkey: signerGroup, isSigner: false, isWritable: false },
      { pubkey: AUDIUS_PROGRAM, isSigner: false, isWritable: false },
      { pubkey: INSTRUCTIONS_PROGRAM, isSigner: false, isWritable: false }
    ],
    programId: CREATE_AND_VERIFY_PROGRAM,
    data: serializedInstructionArgs
  })

  let txInfo = await solanaWeb3.sendAndConfirmTransaction(
    devnetConnection,
    transaction,
    [feePayer]
  )
  console.log(txInfo)
}

exports.createAndVerifyMessage = createAndVerifyMessage