import logging
logging.basicConfig(level=logging.INFO)
from app import get_system_state

state = get_system_state()
if state.broker:
    token = state.broker.get_instrument_token(tradingsymbol="NIFTY BANK", exchange="NSE")
    print(f"NIFTY BANK Token: {token}")
else:
    print("Broker not initialized in system state")
