from typing import List, Optional, Set

import aiosqlite

from chia.types.blockchain_format.coin import Coin
from chia.types.blockchain_format.sized_bytes import bytes32
from chia.types.coin_record import CoinRecord
from chia.util.db_wrapper import DBWrapper
from chia.util.ints import uint32, uint64
from chia.util.lru_cache import LRUCache
from time import time
import logging

log = logging.getLogger(__name__)


class CoinStore:
    """
    This object handles CoinRecords in DB.
    A cache is maintained for quicker access to recent coins.
    """

    coin_record_db: aiosqlite.Connection
    coin_record_cache: LRUCache
    cache_size: uint32
    db_wrapper: DBWrapper

    @classmethod
    async def create(cls, db_wrapper: DBWrapper, cache_size: uint32 = uint32(60000)):
        self = cls()

        self.cache_size = cache_size
        self.db_wrapper = db_wrapper
        self.coin_record_db = db_wrapper.db
        # the coin_name is unique in this table because the CoinStore always
        # only represent a single peak
        await self.coin_record_db.execute(
            (
                "CREATE TABLE IF NOT EXISTS coin_record("
                "coin_name text PRIMARY KEY,"
                " confirmed_index bigint,"
                " spent_index bigint,"
                " spent int,"
                " coinbase int,"
                " puzzle_hash text,"
                " coin_parent text,"
                " amount blob,"
                " timestamp bigint)"
            )
        )

        # Useful for reorg lookups
        await self.coin_record_db.execute(
            "CREATE INDEX IF NOT EXISTS coin_confirmed_index on coin_record(confirmed_index)"
        )

        await self.coin_record_db.execute("CREATE INDEX IF NOT EXISTS coin_spent_index on coin_record(spent_index)")

        await self.coin_record_db.execute("CREATE INDEX IF NOT EXISTS coin_spent on coin_record(spent)")

        await self.coin_record_db.execute("CREATE INDEX IF NOT EXISTS coin_puzzle_hash on coin_record(puzzle_hash)")

        await self.coin_record_db.commit()
        self.coin_record_cache = LRUCache(cache_size)
        return self

    async def new_block(
        self,
        height: uint32,
        timestamp: uint64,
        included_reward_coins: Set[Coin],
        tx_additions: List[Coin],
        tx_removals: List[bytes32],
    ):
        """
        Only called for blocks which are blocks (and thus have rewards and transactions)
        """

        start = time()

        for coin in tx_additions:
            record: CoinRecord = CoinRecord(
                coin,
                height,
                uint32(0),
                False,
                False,
                timestamp,
            )
            await self._add_coin_record(record)

        if height == 0:
            assert len(included_reward_coins) == 0
        else:
            assert len(included_reward_coins) >= 2

        for coin in included_reward_coins:
            reward_coin_r: CoinRecord = CoinRecord(
                coin,
                height,
                uint32(0),
                False,
                True,
                timestamp,
            )
            await self._add_coin_record(reward_coin_r)

        for coin_name in tx_removals:
            await self._set_spent(coin_name, height)

        end = time()
        if end - start > 10:
            log.warning(
                f"It took {end - start:0.2}s to apply {len(tx_additions)} additions and "
                + f"{len(tx_removals)} removals to the coin store. Make sure "
                + "blockchain database is on a fast drive"
            )

    # Checks DB and DiffStores for CoinRecord with coin_name and returns it
    async def get_coin_record(self, coin_name: bytes32) -> Optional[CoinRecord]:
        cached = self.coin_record_cache.get(coin_name)
        if cached is not None:
            return cached
        cursor = await self.coin_record_db.execute("SELECT * from coin_record WHERE coin_name=?", (coin_name.hex(),))
        row = await cursor.fetchone()
        await cursor.close()
        if row is not None:
            coin = Coin(bytes32(bytes.fromhex(row[6])), bytes32(bytes.fromhex(row[5])), uint64.from_bytes(row[7]))
            record = CoinRecord(coin, row[1], row[2], row[3], row[4], row[8])
            self.coin_record_cache.put(record.coin.name(), record)
            return record
        return None

    async def get_coins_added_at_height(self, height: uint32) -> List[CoinRecord]:
        cursor = await self.coin_record_db.execute("SELECT * from coin_record WHERE confirmed_index=?", (height,))
        rows = await cursor.fetchall()
        await cursor.close()
        coins = []
        for row in rows:
            coin = Coin(bytes32(bytes.fromhex(row[6])), bytes32(bytes.fromhex(row[5])), uint64.from_bytes(row[7]))
            coins.append(CoinRecord(coin, row[1], row[2], row[3], row[4], row[8]))
        return coins

    async def get_coins_removed_at_height(self, height: uint32) -> List[CoinRecord]:
        # Special case to avoid querying all unspent coins (spent_index=0)
        if height == 0:
            return []
        cursor = await self.coin_record_db.execute("SELECT * from coin_record WHERE spent_index=?", (height,))
        rows = await cursor.fetchall()
        await cursor.close()
        coins = []
        for row in rows:
            spent: bool = bool(row[3])
            if spent:
                coin = Coin(bytes32(bytes.fromhex(row[6])), bytes32(bytes.fromhex(row[5])), uint64.from_bytes(row[7]))
                coin_record = CoinRecord(coin, row[1], row[2], spent, row[4], row[8])
                coins.append(coin_record)
        return coins

    # Checks DB and DiffStores for CoinRecords with puzzle_hash and returns them
    async def get_coin_records_by_puzzle_hash(
        self,
        include_spent_coins: bool,
        puzzle_hash: bytes32,
        start_height: uint32 = uint32(0),
        end_height: uint32 = uint32((2 ** 32) - 1),
    ) -> List[CoinRecord]:

        coins = set()
        cursor = await self.coin_record_db.execute(
            f"SELECT * from coin_record INDEXED BY coin_puzzle_hash WHERE puzzle_hash=? "
            f"AND confirmed_index>=? AND confirmed_index<? "
            f"{'' if include_spent_coins else 'AND spent=0'}",
            (puzzle_hash.hex(), start_height, end_height),
        )
        rows = await cursor.fetchall()

        await cursor.close()
        for row in rows:
            coin = Coin(bytes32(bytes.fromhex(row[6])), bytes32(bytes.fromhex(row[5])), uint64.from_bytes(row[7]))
            coins.add(CoinRecord(coin, row[1], row[2], row[3], row[4], row[8]))
        return list(coins)

    async def get_coin_records_by_puzzle_hashes(
        self,
        include_spent_coins: bool,
        puzzle_hashes: List[bytes32],
        start_height: uint32 = uint32(0),
        end_height: uint32 = uint32((2 ** 32) - 1),
    ) -> List[CoinRecord]:
        if len(puzzle_hashes) == 0:
            return []

        coins = set()
        puzzle_hashes_db = tuple([ph.hex() for ph in puzzle_hashes])
        cursor = await self.coin_record_db.execute(
            f"SELECT * from coin_record INDEXED BY coin_puzzle_hash "
            f'WHERE puzzle_hash in ({"?," * (len(puzzle_hashes_db) - 1)}?) '
            f"AND confirmed_index>=? AND confirmed_index<? "
            f"{'' if include_spent_coins else 'AND spent=0'}",
            puzzle_hashes_db + (start_height, end_height),
        )

        rows = await cursor.fetchall()

        await cursor.close()
        for row in rows:
            coin = Coin(bytes32(bytes.fromhex(row[6])), bytes32(bytes.fromhex(row[5])), uint64.from_bytes(row[7]))
            coins.add(CoinRecord(coin, row[1], row[2], row[3], row[4], row[8]))
        return list(coins)

    async def get_coin_records_by_names(
        self,
        include_spent_coins: bool,
        names: List[bytes32],
        start_height: uint32 = uint32(0),
        end_height: uint32 = uint32((2 ** 32) - 1),
    ) -> List[CoinRecord]:
        if len(names) == 0:
            return []

        coins = set()
        names_db = tuple([name.hex() for name in names])
        cursor = await self.coin_record_db.execute(
            f'SELECT * from coin_record WHERE coin_name in ({"?," * (len(names_db) - 1)}?) '
            f"AND confirmed_index>=? AND confirmed_index<? "
            f"{'' if include_spent_coins else 'AND spent=0'}",
            names_db + (start_height, end_height),
        )

        rows = await cursor.fetchall()

        await cursor.close()
        for row in rows:
            coin = Coin(bytes32(bytes.fromhex(row[6])), bytes32(bytes.fromhex(row[5])), uint64.from_bytes(row[7]))
            coins.add(CoinRecord(coin, row[1], row[2], row[3], row[4], row[8]))
        return list(coins)

    async def get_coin_records_by_parent_ids(
        self,
        include_spent_coins: bool,
        parent_ids: List[bytes32],
        start_height: uint32 = uint32(0),
        end_height: uint32 = uint32((2 ** 32) - 1),
    ) -> List[CoinRecord]:
        if len(parent_ids) == 0:
            return []

        coins = set()
        parent_ids_db = tuple([pid.hex() for pid in parent_ids])
        cursor = await self.coin_record_db.execute(
            f'SELECT * from coin_record WHERE coin_parent in ({"?," * (len(parent_ids_db) - 1)}?) '
            f"AND confirmed_index>=? AND confirmed_index<? "
            f"{'' if include_spent_coins else 'AND spent=0'}",
            parent_ids_db + (start_height, end_height),
        )

        rows = await cursor.fetchall()

        await cursor.close()
        for row in rows:
            coin = Coin(bytes32(bytes.fromhex(row[6])), bytes32(bytes.fromhex(row[5])), uint64.from_bytes(row[7]))
            coins.add(CoinRecord(coin, row[1], row[2], row[3], row[4], row[8]))
        return list(coins)

    async def rollback_to_block(self, block_index: int):
        """
        Note that block_index can be negative, in which case everything is rolled back
        """
        # Update memory cache
        delete_queue: bytes32 = []
        for coin_name, coin_record in list(self.coin_record_cache.cache.items()):
            if int(coin_record.spent_block_index) > block_index:
                new_record = CoinRecord(
                    coin_record.coin,
                    coin_record.confirmed_block_index,
                    uint32(0),
                    False,
                    coin_record.coinbase,
                    coin_record.timestamp,
                )
                self.coin_record_cache.put(coin_record.coin.name(), new_record)
            if int(coin_record.confirmed_block_index) > block_index:
                delete_queue.append(coin_name)

        for coin_name in delete_queue:
            self.coin_record_cache.remove(coin_name)

        # Delete from storage
        c1 = await self.coin_record_db.execute("DELETE FROM coin_record WHERE confirmed_index>?", (block_index,))
        await c1.close()
        c2 = await self.coin_record_db.execute(
            "UPDATE coin_record SET spent_index = 0, spent = 0 WHERE spent_index>?",
            (block_index,),
        )
        await c2.close()

    # Store CoinRecord in DB and ram cache
    async def _add_coin_record(self, record: CoinRecord) -> None:
        if self.coin_record_cache.get(record.coin.name()) is not None:
            self.coin_record_cache.remove(record.coin.name())

        cursor = await self.coin_record_db.execute(
            "INSERT INTO coin_record VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                record.coin.name().hex(),
                record.confirmed_block_index,
                record.spent_block_index,
                int(record.spent),
                int(record.coinbase),
                str(record.coin.puzzle_hash.hex()),
                str(record.coin.parent_coin_info.hex()),
                bytes(record.coin.amount),
                record.timestamp,
            ),
        )
        await cursor.close()

    # Update coin_record to be spent in DB
    async def _set_spent(self, coin_name: bytes32, index: uint32):

        # if this coin is in the cache, mark it as spent in there
        r = self.coin_record_cache.get(coin_name)
        if r is not None:
            self.coin_record_cache.put(
                r.name, CoinRecord(r.coin, r.confirmed_block_index, index, True, r.coinbase, r.timestamp)
            )

        await self.coin_record_db.execute(
            "UPDATE OR FAIL coin_record SET spent=1,spent_index=? WHERE coin_name=?", (index, coin_name.hex())
        )
