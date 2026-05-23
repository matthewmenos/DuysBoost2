"""
crypto_engine.py — On-chain USDT verification and transfer for DUYS Boost
==========================================================================
Supports three networks:
  • BSC   (BNB Smart Chain)    — EVM, JSON-RPC, eth_getTransactionReceipt
  • Avalanche C-Chain          — EVM, JSON-RPC, eth_getTransactionReceipt
  • Aptos                      — REST API, /transactions/{hash}

Deposits:
  verify_deposit(network, tx_hash, expected_recipient, min_amount_usd)
    → {'ok': bool, 'amount': float, 'error': str}
  Queries the chain to confirm:
    1. TX exists and is finalized (status=1 / success)
    2. The recipient/to address matches our platform wallet
    3. The token is USDT (checks against known contract addresses)
    4. The credited amount meets the declared minimum

Withdrawals:
  send_usdt(network, private_key, to_address, amount_usdt)
    → {'ok': bool, 'tx_hash': str, 'error': str}
  Signs and broadcasts a USDT transfer on the chosen network.
  Uses raw JSON-RPC for EVM chains (no web3 dependency) and the
  Aptos REST API for Aptos.

All amounts are in USD / USDT (6 decimal token, but we work in float).
"""

import json
import os
import struct
import time
import logging
import requests

logger = logging.getLogger(__name__)

# ─────────────────────────────────────────────────────────────────────────────
# Network configuration
# ─────────────────────────────────────────────────────────────────────────────

# Public RPC endpoints — override with your own (e.g. Alchemy/QuickNode) via env
BSC_RPC      = os.environ.get('BSC_RPC_URL',       'https://bsc-dataseed1.binance.org/')
AVAX_RPC     = os.environ.get('AVAX_RPC_URL',      'https://api.avax.network/ext/bc/C/rpc')
APTOS_RPC    = os.environ.get('APTOS_RPC_URL',     'https://fullnode.mainnet.aptoslabs.com/v1')

# USDT contract addresses (checksummed EVM, canonical Aptos coin type)
USDT_CONTRACTS = {
    'bsc':       '0x55d398326f99059fF775485246999027B3197955',   # BSC USDT (BEP-20)
    'avalanche': '0x9702230A8Ea53601f5cD2dc00fDBc13d4dF4A8c7',   # Avalanche USDT (native)
    # Aptos USDT — LayerZero bridged (most common)
    'aptos':     '0xf22bede237a07cfa3450f6e36cf5ed9aa9cb6b4b7c8be3dddce5b9ecb1c1e1c::asset::USDT',
}

# How many on-chain confirmations we require before crediting
REQUIRED_CONFIRMATIONS = {
    'bsc':       12,
    'avalanche': 1,   # Avalanche finalizes in ~1-2s after inclusion
    'aptos':     1,
}

RPC_TIMEOUT = 20  # seconds per RPC call
_RPC_ID     = 0   # monotonic JSON-RPC id counter


def _rpc_id():
    global _RPC_ID
    _RPC_ID += 1
    return _RPC_ID


# ─────────────────────────────────────────────────────────────────────────────
# Low-level EVM JSON-RPC helpers (no web3 needed)
# ─────────────────────────────────────────────────────────────────────────────

def _evm_call(rpc_url: str, method: str, params: list):
    """
    Fire a single JSON-RPC request. Returns (result, error_msg).
    result is None on error; error_msg is None on success.
    """
    payload = {
        'jsonrpc': '2.0',
        'id': _rpc_id(),
        'method': method,
        'params': params,
    }
    try:
        r = requests.post(rpc_url, json=payload, timeout=RPC_TIMEOUT,
                          headers={'Content-Type': 'application/json'})
        body = r.json()
    except requests.RequestException as e:
        return None, f'RPC network error: {e}'
    except ValueError:
        return None, 'RPC returned non-JSON response'
    if 'error' in body:
        return None, body['error'].get('message', str(body['error']))
    return body.get('result'), None


def _evm_get_tx_receipt(rpc_url: str, tx_hash: str):
    """Returns (receipt_dict | None, error_str | None)."""
    return _evm_call(rpc_url, 'eth_getTransactionReceipt', [tx_hash])


def _evm_get_tx(rpc_url: str, tx_hash: str):
    """Returns (tx_dict | None, error_str | None)."""
    return _evm_call(rpc_url, 'eth_getTransactionByHash', [tx_hash])


def _evm_block_number(rpc_url: str):
    result, err = _evm_call(rpc_url, 'eth_blockNumber', [])
    if err or result is None:
        return None, err or 'No result'
    return int(result, 16), None


def _evm_get_chain_id(rpc_url: str):
    result, err = _evm_call(rpc_url, 'eth_chainId', [])
    if err or result is None:
        return None, err
    return int(result, 16), None


def _evm_gas_price(rpc_url: str):
    result, err = _evm_call(rpc_url, 'eth_gasPrice', [])
    if err or result is None:
        return None, err
    return int(result, 16), None


def _evm_get_nonce(rpc_url: str, address: str):
    result, err = _evm_call(rpc_url, 'eth_getTransactionCount', [address, 'latest'])
    if err or result is None:
        return None, err
    return int(result, 16), None


def _evm_send_raw(rpc_url: str, raw_hex: str):
    return _evm_call(rpc_url, 'eth_sendRawTransaction', [raw_hex])


# ─────────────────────────────────────────────────────────────────────────────
# ERC-20 / BEP-20 log decoder
# ─────────────────────────────────────────────────────────────────────────────

# Transfer(address indexed from, address indexed to, uint256 value)
TRANSFER_TOPIC = '0xddf252ad1be2c89b69c2b068fc378daa952ba7f163c4a11628f55a4df523b3ef'


def _decode_erc20_transfer_logs(receipt: dict, contract_addr: str):
    """
    Return list of {'from': addr, 'to': addr, 'value': int_wei} for each
    ERC-20 Transfer event from the given contract in this receipt.
    """
    transfers = []
    contract_lower = contract_addr.lower()
    for log in (receipt.get('logs') or []):
        if log.get('address', '').lower() != contract_lower:
            continue
        topics = log.get('topics') or []
        if len(topics) < 3 or topics[0].lower() != TRANSFER_TOPIC:
            continue
        # topics[1] = from (padded), topics[2] = to (padded)
        from_addr = '0x' + topics[1][-40:]
        to_addr   = '0x' + topics[2][-40:]
        data = log.get('data', '0x')
        value = int(data, 16) if data and data != '0x' else 0
        transfers.append({'from': from_addr, 'to': to_addr, 'value': value})
    return transfers


# ─────────────────────────────────────────────────────────────────────────────
# EVM transaction signer (keccak + secp256k1 via eth_account or pure python)
# ─────────────────────────────────────────────────────────────────────────────

def _sign_and_send_evm(rpc_url: str, chain_id: int, private_key_hex: str,
                        to_addr: str, contract_addr: str, amount_tokens: int,
                        gas_limit: int = 65_000):
    """
    Build, sign and broadcast an ERC-20 transfer call.
    Uses eth_account if available, otherwise falls back to a minimal
    pure-Python RLP + secp256k1 signer (requires the `coincurve` library).
    Returns (tx_hash_hex | None, error_str | None).
    """
    # ── Try eth_account (ships with web3; not always installed) ──────────
    try:
        from eth_account import Account as EthAccount
        from eth_account.messages import encode_defunct
        # encode ERC-20 transfer(address,uint256) call
        selector = bytes.fromhex('a9059cbb')
        to_padded = bytes.fromhex(to_addr[2:].zfill(64))
        amount_padded = amount_tokens.to_bytes(32, 'big')
        data = '0x' + (selector + to_padded + amount_padded).hex()

        nonce, err = _evm_get_nonce(rpc_url, EthAccount.from_key(private_key_hex).address)
        if err:
            return None, f'nonce error: {err}'
        gas_price, err = _evm_gas_price(rpc_url)
        if err:
            return None, f'gas price error: {err}'

        tx = {
            'nonce':    nonce,
            'gasPrice': gas_price,
            'gas':      gas_limit,
            'to':       contract_addr,
            'value':    0,
            'data':     data,
            'chainId':  chain_id,
        }
        signed = EthAccount.sign_transaction(tx, private_key_hex)
        raw = '0x' + signed.raw_transaction.hex()
        result, err = _evm_send_raw(rpc_url, raw)
        if err:
            return None, f'broadcast error: {err}'
        return result, None

    except ImportError:
        pass  # eth_account not available

    # ── Fallback: coincurve + minimal RLP ────────────────────────────────
    try:
        import coincurve
        from hashlib import sha3_256 as _sha3   # keccak256 via pysha3 or pycryptodome
        try:
            from Crypto.Hash import keccak as _keccak
            def keccak256(b): k = _keccak.new(digest_bits=256); k.update(b); return k.digest()
        except ImportError:
            try:
                import sha3 as _sha3mod
                def keccak256(b): return _sha3mod.keccak_256(b).digest()
            except ImportError:
                return None, ('eth_account and a keccak library (pycryptodome or pysha3) '
                              'are required for signing. Install: pip install eth-account')

        pk_bytes = bytes.fromhex(private_key_hex.lstrip('0x'))
        priv = coincurve.PrivateKey(pk_bytes)
        # derive address
        pub = priv.public_key.format(compressed=False)[1:]  # 64 bytes
        addr_hex = '0x' + keccak256(pub)[-20:].hex()

        nonce, err = _evm_get_nonce(rpc_url, addr_hex)
        if err: return None, f'nonce error: {err}'
        gas_price, err = _evm_gas_price(rpc_url)
        if err: return None, f'gas price error: {err}'

        selector = bytes.fromhex('a9059cbb')
        to_padded = bytes.fromhex(to_addr[2:].zfill(64))
        amount_padded = amount_tokens.to_bytes(32, 'big')
        data_bytes = selector + to_padded + amount_padded

        def rlp_encode(item):
            if isinstance(item, bytes):
                if len(item) == 1 and item[0] < 0x80:
                    return item
                prefix = len(item)
                if prefix <= 55:
                    return bytes([0x80 + prefix]) + item
                pl = prefix.to_bytes((prefix.bit_length() + 7) // 8, 'big')
                return bytes([0xb7 + len(pl)]) + pl + item
            if isinstance(item, list):
                payload = b''.join(rlp_encode(i) for i in item)
                lp = len(payload)
                if lp <= 55:
                    return bytes([0xc0 + lp]) + payload
                ll = lp.to_bytes((lp.bit_length() + 7) // 8, 'big')
                return bytes([0xf7 + len(ll)]) + ll + payload

        def int_to_bytes(n): return b'' if n == 0 else n.to_bytes((n.bit_length() + 7) // 8, 'big')

        raw_tx = [
            int_to_bytes(nonce),
            int_to_bytes(gas_price),
            int_to_bytes(gas_limit),
            bytes.fromhex(contract_addr[2:]),
            b'',  # value = 0
            data_bytes,
            int_to_bytes(chain_id), b'', b'',
        ]
        encoded = rlp_encode(raw_tx)
        tx_hash_bytes = keccak256(encoded)
        sig = priv.sign_recoverable(tx_hash_bytes, hasher=None)
        r = sig[:32]; s = sig[32:64]; v_raw = sig[64]
        v = v_raw + chain_id * 2 + 35

        signed_tx = [
            int_to_bytes(nonce), int_to_bytes(gas_price), int_to_bytes(gas_limit),
            bytes.fromhex(contract_addr[2:]), b'', data_bytes,
            int_to_bytes(v), r, s,
        ]
        raw_hex = '0x' + rlp_encode(signed_tx).hex()
        result, err = _evm_send_raw(rpc_url, raw_hex)
        if err: return None, f'broadcast error: {err}'
        return result, None

    except ImportError:
        return None, ('No signing library available. '
                      'Install eth-account: pip install eth-account')


# ─────────────────────────────────────────────────────────────────────────────
# Aptos REST helpers
# ─────────────────────────────────────────────────────────────────────────────

def _aptos_get_tx(tx_hash: str):
    url = f'{APTOS_RPC}/transactions/by_hash/{tx_hash}'
    try:
        r = requests.get(url, timeout=RPC_TIMEOUT)
        if r.status_code == 404:
            return None, 'Transaction not found'
        if r.status_code != 200:
            return None, f'Aptos API error {r.status_code}'
        return r.json(), None
    except requests.RequestException as e:
        return None, f'Aptos network error: {e}'


def _aptos_get_account_info(address: str):
    url = f'{APTOS_RPC}/accounts/{address}'
    try:
        r = requests.get(url, timeout=RPC_TIMEOUT)
        if r.status_code != 200:
            return None, f'Aptos account error {r.status_code}'
        return r.json(), None
    except requests.RequestException as e:
        return None, f'Aptos network error: {e}'


def _aptos_get_coin_balance(address: str, coin_type: str):
    """Returns (balance_int, error). Balance is in the coin's smallest unit."""
    resource_type = f'0x1::coin::CoinStore<{coin_type}>'
    url = f'{APTOS_RPC}/accounts/{address}/resource/{requests.utils.quote(resource_type, safe="")}'
    try:
        r = requests.get(url, timeout=RPC_TIMEOUT)
        if r.status_code == 404:
            return 0, None
        if r.status_code != 200:
            return None, f'Aptos resource error {r.status_code}'
        data = r.json()
        value = int(data['data']['coin']['value'])
        return value, None
    except (requests.RequestException, KeyError, ValueError) as e:
        return None, f'Aptos balance error: {e}'


def _aptos_submit_tx(payload_dict: dict, private_key_hex: str, sender: str):
    """
    Encode, sign and submit an Aptos entry function transaction.
    Requires the aptos-sdk package.
    Returns (tx_hash | None, error | None).
    """
    try:
        from aptos_sdk.account import Account as AptosAccount
        from aptos_sdk.client import RestClient
        from aptos_sdk.transactions import (
            EntryFunction, TransactionArgument,
            TransactionPayload, RawTransaction,
            SignedTransaction, Authenticator, Ed25519Authenticator
        )
        from aptos_sdk.type_tag import TypeTag, StructTag
        from aptos_sdk.bcs import Serializer

        client = RestClient(APTOS_RPC)
        account = AptosAccount.load_key(private_key_hex)
        client.submit_transaction(account, payload_dict)
    except ImportError:
        # aptos-sdk not installed — use raw REST API approach
        pass
    except Exception as e:
        return None, str(e)

    # Fallback: call Aptos REST API to encode + simulate + submit
    try:
        acct_info, err = _aptos_get_account_info(sender)
        if err:
            return None, err
        seq_num = int(acct_info['sequence_number'])
        exp_ts = int(time.time()) + 600

        encode_url = f'{APTOS_RPC}/transactions/encode_submission'
        submission_body = {
            'sender': sender,
            'sequence_number': str(seq_num),
            'max_gas_amount': '20000',
            'gas_unit_price': '100',
            'expiration_timestamp_secs': str(exp_ts),
            'payload': payload_dict,
        }
        r = requests.post(encode_url, json=submission_body, timeout=RPC_TIMEOUT)
        if r.status_code != 200:
            return None, f'Aptos encode error: {r.text[:200]}'
        msg_hex = r.json()  # hex string of bytes to sign

        # sign with ed25519
        msg_bytes = bytes.fromhex(msg_hex[2:] if msg_hex.startswith('0x') else msg_hex)
        try:
            from aptos_sdk.account import Account as AptosAccount
            acct = AptosAccount.load_key(private_key_hex)
            sig_bytes = acct.sign(msg_bytes).data()
            pub_bytes = acct.public_key().key.encode()
        except ImportError:
            try:
                import ed25519
                sk = ed25519.SigningKey(bytes.fromhex(private_key_hex))
                sig_bytes = sk.sign(msg_bytes)
                pub_bytes = sk.get_verifying_key().to_bytes()
            except ImportError:
                return None, ('aptos-sdk or ed25519 library required for Aptos signing. '
                              'Install: pip install aptos-sdk')

        submit_body = {
            **submission_body,
            'signature': {
                'type': 'ed25519_signature',
                'public_key': '0x' + pub_bytes.hex(),
                'signature': '0x' + sig_bytes.hex(),
            }
        }
        r2 = requests.post(f'{APTOS_RPC}/transactions', json=submit_body, timeout=RPC_TIMEOUT)
        if r2.status_code not in (200, 202):
            return None, f'Aptos submit error: {r2.text[:300]}'
        data = r2.json()
        return data.get('hash'), None

    except Exception as e:
        return None, f'Aptos submission failed: {e}'


# ─────────────────────────────────────────────────────────────────────────────
# Public API: verify_deposit
# ─────────────────────────────────────────────────────────────────────────────

def verify_deposit(network: str, tx_hash: str, expected_recipient: str,
                   min_amount_usd: float = 0.01) -> dict:
    """
    Verify a USDT deposit on-chain.

    Returns:
        {'ok': True,  'amount': 12.50, 'error': ''}   on success
        {'ok': False, 'amount': 0,     'error': '...'}  on failure

    Checks:
      1. TX is finalized (success status on chain)
      2. Required confirmations have passed
      3. The USDT token was transferred TO our platform wallet
      4. Amount ≥ min_amount_usd
    """
    tx_hash = tx_hash.strip()
    if not tx_hash:
        return {'ok': False, 'amount': 0, 'error': 'Empty transaction hash'}

    if network in ('bsc', 'avalanche'):
        return _verify_evm_deposit(network, tx_hash, expected_recipient, min_amount_usd)
    elif network == 'aptos':
        return _verify_aptos_deposit(tx_hash, expected_recipient, min_amount_usd)
    else:
        return {'ok': False, 'amount': 0, 'error': f'Unknown network: {network}'}


def _verify_evm_deposit(network: str, tx_hash: str, expected_recipient: str,
                         min_amount_usd: float) -> dict:
    rpc_url = BSC_RPC if network == 'bsc' else AVAX_RPC
    contract = USDT_CONTRACTS[network]
    required_confs = REQUIRED_CONFIRMATIONS[network]

    # Normalize hash
    if not tx_hash.startswith('0x'):
        tx_hash = '0x' + tx_hash

    # 1. Get receipt
    receipt, err = _evm_get_tx_receipt(rpc_url, tx_hash)
    if err:
        return {'ok': False, 'amount': 0, 'error': f'Could not fetch receipt: {err}'}
    if receipt is None:
        return {'ok': False, 'amount': 0, 'error': 'Transaction not found on chain (may still be pending)'}

    # 2. Check success
    status = receipt.get('status')
    if status is None:
        return {'ok': False, 'amount': 0, 'error': 'Transaction status unknown (pre-Byzantium block?)'}
    if int(status, 16) != 1:
        return {'ok': False, 'amount': 0, 'error': 'Transaction failed on chain (reverted)'}

    # 3. Check confirmations
    tx_block = receipt.get('blockNumber')
    if tx_block:
        current_block, err = _evm_block_number(rpc_url)
        if err is None and current_block is not None:
            confs = current_block - int(tx_block, 16)
            if confs < required_confs:
                return {
                    'ok': False, 'amount': 0,
                    'error': f'Only {confs}/{required_confs} confirmations so far. Please wait.'
                }

    # 4. Decode ERC-20 Transfer logs
    transfers = _decode_erc20_transfer_logs(receipt, contract)
    if not transfers:
        return {
            'ok': False, 'amount': 0,
            'error': f'No USDT ({contract[:10]}…) Transfer event found in this transaction.'
        }

    # 5. Find transfer TO our wallet
    recipient_lower = expected_recipient.lower()
    matched_value = 0
    for t in transfers:
        if t['to'].lower() == recipient_lower:
            matched_value += t['value']

    if matched_value == 0:
        return {
            'ok': False, 'amount': 0,
            'error': (f'USDT was not transferred to the platform wallet ({expected_recipient[:8]}…). '
                      'Please send to the correct address.')
        }

    # USDT on BSC and Avalanche both use 6 decimals
    amount_usd = matched_value / 1_000_000

    if amount_usd < min_amount_usd:
        return {
            'ok': False, 'amount': amount_usd,
            'error': f'Amount too small: ${amount_usd:.6f} < minimum ${min_amount_usd:.2f}'
        }

    return {'ok': True, 'amount': round(amount_usd, 6), 'error': ''}


def _verify_aptos_deposit(tx_hash: str, expected_recipient: str,
                           min_amount_usd: float) -> dict:
    tx, err = _aptos_get_tx(tx_hash)
    if err:
        return {'ok': False, 'amount': 0, 'error': f'Could not fetch transaction: {err}'}

    # Check success
    vm_status = tx.get('vm_status', '')
    if vm_status != 'Executed successfully':
        return {'ok': False, 'amount': 0, 'error': f'Transaction did not succeed: {vm_status}'}

    usdt_type = USDT_CONTRACTS['aptos']
    recipient_lower = expected_recipient.lower()
    matched_value = 0

    # Scan CoinWithdraw / CoinDeposit events  
    for event in (tx.get('events') or []):
        etype = event.get('type', '')
        # Look for coin deposit event for USDT to our address
        if '::coin::DepositEvent' not in etype and '::coin::CoinDeposit' not in etype:
            continue
        # Check the account associated with this event
        key = event.get('key', '') or event.get('guid', {}).get('account_address', '')
        if key and recipient_lower not in key.lower():
            continue
        data = event.get('data') or {}
        coin_type = data.get('coin_type') or etype
        if usdt_type.lower() not in coin_type.lower():
            # Also check changes block
            continue
        amount_raw = data.get('amount', 0)
        matched_value += int(amount_raw)

    # Also scan `changes` array for resource writes (more reliable)
    if matched_value == 0:
        for change in (tx.get('changes') or []):
            if change.get('address', '').lower() != recipient_lower:
                continue
            ctype = change.get('data', {}).get('type', '')
            if usdt_type.lower() not in ctype.lower():
                continue
            # CoinStore write — check the coin data
            coin_data = change.get('data', {}).get('data', {})
            coin_value = coin_data.get('coin', {}).get('value')
            if coin_value is not None:
                # this is the final balance, not the delta — can't use directly
                pass

    # Fallback: scan payload arguments when it's a simple coin transfer
    if matched_value == 0:
        payload = tx.get('payload') or {}
        fn = payload.get('function', '')
        if '::coin::transfer' in fn or '::aptos_account::transfer_coins' in fn:
            args = payload.get('arguments') or []
            type_args = payload.get('type_arguments') or []
            if (len(args) >= 2 and type_args
                    and usdt_type.lower() in type_args[0].lower()
                    and args[0].lower() == recipient_lower):
                matched_value = int(args[1])

    if matched_value == 0:
        return {
            'ok': False, 'amount': 0,
            'error': f'No USDT transfer to platform wallet found in this transaction.'
        }

    # Aptos USDT (LayerZero) uses 6 decimals
    amount_usd = matched_value / 1_000_000

    if amount_usd < min_amount_usd:
        return {
            'ok': False, 'amount': amount_usd,
            'error': f'Amount too small: ${amount_usd:.6f} < minimum ${min_amount_usd:.2f}'
        }

    return {'ok': True, 'amount': round(amount_usd, 6), 'error': ''}


# ─────────────────────────────────────────────────────────────────────────────
# Public API: send_usdt
# ─────────────────────────────────────────────────────────────────────────────

def send_usdt(network: str, private_key: str, to_address: str,
              amount_usd: float) -> dict:
    """
    Send USDT to a user's wallet on the given network.

    Returns:
        {'ok': True,  'tx_hash': '0x...', 'error': ''}
        {'ok': False, 'tx_hash': '',       'error': '...'}

    amount_usd is in USD (e.g. 5.50 means 5.50 USDT).
    """
    if not private_key:
        return {'ok': False, 'tx_hash': '', 'error': 'Withdrawal private key not configured'}
    if amount_usd <= 0:
        return {'ok': False, 'tx_hash': '', 'error': 'Amount must be > 0'}

    if network in ('bsc', 'avalanche'):
        return _send_evm_usdt(network, private_key, to_address, amount_usd)
    elif network == 'aptos':
        return _send_aptos_usdt(private_key, to_address, amount_usd)
    else:
        return {'ok': False, 'tx_hash': '', 'error': f'Unknown network: {network}'}


def _send_evm_usdt(network: str, private_key: str, to_address: str,
                    amount_usd: float) -> dict:
    rpc_url  = BSC_RPC if network == 'bsc' else AVAX_RPC
    contract = USDT_CONTRACTS[network]

    chain_id, err = _evm_get_chain_id(rpc_url)
    if err:
        return {'ok': False, 'tx_hash': '', 'error': f'Could not get chain ID: {err}'}

    # USDT uses 6 decimals on both BSC and Avalanche
    token_amount = int(amount_usd * 1_000_000)

    # Normalize private key
    pk = private_key.strip()
    if pk.startswith('0x'):
        pk = pk[2:]

    tx_hash, err = _sign_and_send_evm(rpc_url, chain_id, pk, to_address,
                                       contract, token_amount)
    if err:
        return {'ok': False, 'tx_hash': '', 'error': err}
    if not tx_hash:
        return {'ok': False, 'tx_hash': '', 'error': 'No tx hash returned from RPC'}

    return {'ok': True, 'tx_hash': tx_hash, 'error': ''}


def _send_aptos_usdt(private_key: str, to_address: str, amount_usd: float) -> dict:
    # USDT on Aptos uses 6 decimals
    amount_raw = int(amount_usd * 1_000_000)
    usdt_type  = USDT_CONTRACTS['aptos']

    # Derive sender address from private key
    try:
        from aptos_sdk.account import Account as AptosAccount
        acct = AptosAccount.load_key(private_key.strip())
        sender = str(acct.address())
    except ImportError:
        try:
            import ed25519
            sk = ed25519.SigningKey(bytes.fromhex(private_key.strip()))
            # Aptos address = sha3_256(public_key_bytes + 0x00)
            import hashlib
            pub = sk.get_verifying_key().to_bytes()
            addr_bytes = hashlib.sha3_256(pub + b'\x00').digest()
            sender = '0x' + addr_bytes.hex()
        except ImportError:
            return {
                'ok': False, 'tx_hash': '',
                'error': 'aptos-sdk or ed25519 library required. Install: pip install aptos-sdk'
            }

    payload = {
        'type': 'entry_function_payload',
        'function': '0x1::coin::transfer',
        'type_arguments': [usdt_type],
        'arguments': [to_address, str(amount_raw)],
    }

    tx_hash, err = _aptos_submit_tx(payload, private_key.strip(), sender)
    if err:
        return {'ok': False, 'tx_hash': '', 'error': err}

    return {'ok': True, 'tx_hash': tx_hash or '', 'error': ''}


# ─────────────────────────────────────────────────────────────────────────────
# Utility: get hot-wallet address from private key (for display/logging)
# ─────────────────────────────────────────────────────────────────────────────

def get_evm_address_from_key(private_key_hex: str) -> str | None:
    """Derive the EVM address (0x...) from a hex private key, if possible."""
    pk = private_key_hex.strip().lstrip('0x')
    try:
        from eth_account import Account as EthAccount
        return EthAccount.from_key(pk).address
    except ImportError:
        pass
    try:
        import coincurve
        from Crypto.Hash import keccak as _keccak
        def keccak256(b):
            k = _keccak.new(digest_bits=256); k.update(b); return k.digest()
        priv = coincurve.PrivateKey(bytes.fromhex(pk))
        pub  = priv.public_key.format(compressed=False)[1:]
        return '0x' + keccak256(pub)[-20:].hex()
    except ImportError:
        return None
    
