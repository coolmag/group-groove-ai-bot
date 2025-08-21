# -*- coding: utf-8 -*-
from asyncio import Lock

state_lock = Lock()
status_lock = Lock()
refill_lock = Lock()