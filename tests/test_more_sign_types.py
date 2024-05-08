import os

from trio_binance import AsyncClient

# Generate rsa key pair by 3 steps: generate pairs file, extract pub key and private key
# openssl genrsa -out keypair.pem 2048
# openssl rsa -in keypair.pem -pubout -out publickey.crt
# openssl pkcs8 -topk8 -inform PEM -outform PEM -nocrypt -in keypair.pem -out pkcs8.key


async def test_rsa_signature():
    client: AsyncClient = await AsyncClient.create(
        api_key=os.getenv("BINANCE_RSA_API_KEY"),
        api_secret=os.getenv("BINANCE_RSA_PRIVATE_KEY_PATH"),
        sign_style="RSA",
    )
    async with client:
        account_info = await client.get_account()
        assert isinstance(account_info, dict)
        assert "accountType" in account_info
        assert "uid" in account_info


# Generate ed25519 key pair by 2 steps: generate private pem file and get public key from private key
# openssl genpkey -algorithm ed25519 -out private.pem
# openssl pkey -in private.pem -pubout -out public.pem


async def test_ed25519_signature():
    client: AsyncClient = await AsyncClient.create(
        api_key=os.getenv("BINANCE_ED25519_API_KEY"),
        api_secret=os.getenv("BINANCE_ED25519_PRIVATE_KEY_PATH"),
        sign_style="Ed25519",
    )
    async with client:
        account_info = await client.get_account()
        assert isinstance(account_info, dict)
        assert "accountType" in account_info
        assert "uid" in account_info
