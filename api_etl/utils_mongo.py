"""
Module used to interact with Dynamo databases.
"""

import logging
from urllib.parse import quote_plus

from pymongo import MongoClient
import asyncio
from motor.motor_asyncio import AsyncIOMotorClient

from api_etl.utils_secrets import get_secret

logger = logging.getLogger(__name__)

MONGO_HOST = get_secret("MONGO_HOST")
MONGO_PORT = get_secret("MONGO_PORT")
MONGO_USER = get_secret("MONGO_USER")
MONGO_DB_NAME = get_secret("MONGO_DB_NAME")
MONGO_PASSWORD = get_secret("MONGO_PASSWORD")


def build_mongo_uri(
    host=MONGO_HOST, user=MONGO_USER, password=MONGO_PASSWORD,
    port=MONGO_PORT, database=None
):
    uri = "mongodb://"
    if user and password:
        uri += "%s:%s@" % (quote_plus(user), quote_plus(password))
    uri += host
    if port:
        uri += ":" + str(port)
    if database:
        uri += "/%s" % quote_plus(database)
    return uri


def get_mongoclient(max_delay=15000):
    uri = build_mongo_uri()
    client = MongoClient(uri, serverSelectionTimeoutMS=max_delay)
    return client


def get_async_mongoclient():
    uri = build_mongo_uri()
    client = AsyncIOMotorClient(uri)
    return client


def mongo_get_async_collection(collection):
    c = get_async_mongoclient()
    db = c[MONGO_DB_NAME]
    collection = db[collection]
    return collection


def mongo_get_collection(collection):
    c = get_mongoclient()
    db = c[MONGO_DB_NAME]
    collection = db[collection]
    return collection


def mongo_async_save_chunks(collection, chunks_list):
    asy_collection = mongo_get_async_collection(collection)

    async def do_insert_many(chunk):
        try:
            await asy_collection.insert_many(chunk)
            logger.debug("Chunk inserted")
        except:
            logger.error("Could not save chunk")

    async def run(chunks_list):
        tasks = []
        # Fetch all responses within one Client session,
        # keep connection alive for all requests.
        for chunk in chunks_list:
            task = asyncio.ensure_future(
                do_insert_many(chunk))
            tasks.append(task)

        # all response in this variable
        responses = await asyncio.gather(*tasks)
        return responses

    loop = asyncio.get_event_loop()
    future = asyncio.ensure_future(run(chunks_list))
    loop.run_until_complete(future)
    return future.result()


def mongo_async_upsert_items(collection, item_list, index_fields):
    asy_collection = mongo_get_async_collection(collection)

    def mongo_get_replace_filter(item_to_upsert, index_fields):
        m_filter = {}
        for index_field in index_fields:
            m_filter[index_field] = item_to_upsert[index_field]
        return m_filter

    async def do_upsert(item_to_upsert, m_filter):
        try:
            result = await asy_collection.replace_one(
                m_filter, item_to_upsert, upsert=True
            )
            logger.debug("Item inserted")
            if not result.acknowledged:
                logger.error("Item %s not inserted" % item_to_upsert)

        except Exception as e:
            logger.error("Could not save item, error %s" % e)

    async def run(item_list):
        tasks = []
        for item_to_upsert in item_list:
            m_filter = mongo_get_replace_filter(item_to_upsert, index_fields)
            task = asyncio.ensure_future(
                do_upsert(item_to_upsert, m_filter))
            tasks.append(task)

        responses = await asyncio.gather(*tasks)
        return responses

    loop = asyncio.get_event_loop()
    future = asyncio.ensure_future(run(item_list=item_list))
    loop.run_until_complete(future)
    return future.result()


def mongo_async_update_items(collection, item_query_update_list):
    asy_collection = mongo_get_async_collection(collection)

    async def do_update(item_query_update):
        try:
            find_query = item_query_update[0]
            update_query = item_query_update[1]
            result = await asy_collection.update_one(find_query, update_query)
            logger.debug("Item updated")
            if not result.acknowledged:
                logger.error("Item %s not updated" % item_query_update)

        except Exception as e:
            logger.error("Could not update item, error %s" % e)

    async def run(item_list):
        tasks = []
        for item_query_update in item_list:
            task = asyncio.ensure_future(do_update(item_query_update))
            tasks.append(task)

        responses = await asyncio.gather(*tasks)
        return responses

    loop = asyncio.get_event_loop()
    future = asyncio.ensure_future(run(item_list=item_query_update_list))
    loop.run_until_complete(future)
    return future.result()
