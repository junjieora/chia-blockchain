import asyncio
import random
from time import time
from pathlib import Path
from chia.full_node.coin_store import CoinStore
from typing import List
import os
import sys

import aiosqlite
from chia.util.db_wrapper import DBWrapper
from chia.consensus.coinbase import create_farmer_coin, create_pool_coin
from chia.consensus.default_constants import DEFAULT_CONSTANTS
from chia.types.blockchain_format.sized_bytes import bytes32
from chia.types.blockchain_format.coin import Coin
from chia.util.ints import uint64


NUM_ITERS = 200


async def setup_db() -> DBWrapper:
    db_filename = Path("coin-store-benchmark.db")
    try:
        os.unlink(db_filename)
    except FileNotFoundError:
        pass
    connection = await aiosqlite.connect(db_filename)
    await connection.execute("pragma journal_mode=wal")
    await connection.execute("pragma synchronous=OFF")
    return DBWrapper(connection)


def rand_hash() -> bytes32:
    return random.randbytes(32)


def make_coin() -> Coin:
    return Coin(rand_hash(), rand_hash(), uint64(1))


async def run_new_block_benchmark():

    db_wrapper: DBWrapper = await setup_db()

    try:
        coin_store = await CoinStore.create(db_wrapper)
        # farmer puzzle hash
        ph = bytes32(b"a" * 32)

        total_time = 0

        all_added: List[bytes32] = []

        total_add = 0
        total_remove = 0
        print("Profiling mostly additions ", end="")
        for height in range(1, NUM_ITERS):
            additions = []
            removals = []

            # add some new coins
            for i in range(2000):
                c = make_coin()
                additions.append(c)
                all_added.append(c.get_hash())
            total_add += 2000

            # remove some coins we've added previously
            random.shuffle(all_added)
            removals = all_added[:100]
            all_added = all_added[100:]
            total_remove += 100

            farmer_coin = create_farmer_coin(height, ph, 250000000, DEFAULT_CONSTANTS.GENESIS_CHALLENGE)
            pool_coin = create_pool_coin(height, ph, 1750000000, DEFAULT_CONSTANTS.GENESIS_CHALLENGE)
            reward_coins = [pool_coin, farmer_coin]
            start = time()
            await coin_store.new_block(
                height,
                1631794488 + height * 19,  # 19 seconds per block
                set(reward_coins),
                additions,
                removals,
            )
            stop = time()
            total_time += stop - start
            print(".", end="")
            sys.stdout.flush()

        print(f"\nMOSTLY ADDITIONS, time: {total_time:0.4f}s additions: {total_add} removals: {total_remove}")

        print("Profiling mostly removals ", end="")
        total_add = 0
        total_remove = 0
        total_time = 0
        for height in range(NUM_ITERS, NUM_ITERS * 2):
            additions = []
            removals = []

            # add one new coins
            c = make_coin()
            additions.append(c)
            all_added.append(c.get_hash())
            total_add += 1

            # remove some coins we've added previously
            random.shuffle(all_added)
            removals = all_added[:700]
            all_added = all_added[700:]
            total_remove += 700

            farmer_coin = create_farmer_coin(height, ph, 250000000, DEFAULT_CONSTANTS.GENESIS_CHALLENGE)
            pool_coin = create_pool_coin(height, ph, 1750000000, DEFAULT_CONSTANTS.GENESIS_CHALLENGE)
            reward_coins = [pool_coin, farmer_coin]
            start = time()
            await coin_store.new_block(
                height,
                1631794488 + height * 19,  # 19 seconds per block
                set(reward_coins),
                additions,
                removals,
            )
            await db_wrapper.db.commit()
            stop = time()
            total_time += stop - start
            print(".", end="")
            sys.stdout.flush()

        print(f"\nMOSTLY REMOVALS, time: {total_time:0.4f}s additions: {total_add} removals: {total_remove}")

        print("Profiling full block transactions", end="")
        total_add = 0
        total_remove = 0
        total_time = 0
        for height in range(NUM_ITERS * 2, NUM_ITERS * 3):
            additions = []
            removals = []

            # add some new coins
            for i in range(2000):
                c = make_coin()
                additions.append(c)
                all_added.append(c.get_hash())
            total_add += 2000

            # remove some coins we've added previously
            random.shuffle(all_added)
            removals = all_added[:2000]
            all_added = all_added[2000:]
            total_remove += 2000

            farmer_coin = create_farmer_coin(height, ph, 250000000, DEFAULT_CONSTANTS.GENESIS_CHALLENGE)
            pool_coin = create_pool_coin(height, ph, 1750000000, DEFAULT_CONSTANTS.GENESIS_CHALLENGE)
            reward_coins = [pool_coin, farmer_coin]
            start = time()
            await coin_store.new_block(
                height,
                1631794488 + height * 19,  # 19 seconds per block
                set(reward_coins),
                additions,
                removals,
            )
            await db_wrapper.db.commit()
            stop = time()
            total_time += stop - start
            print(".", end="")
            sys.stdout.flush()

        print(f"\nFULLBLOCKS, time: {total_time:0.4f}s additions: {total_add} removals: {total_remove}")

    finally:
        await db_wrapper.db.close()


if __name__ == "__main__":
    asyncio.run(run_new_block_benchmark())
