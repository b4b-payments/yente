import asyncio
import threading
from normality import WS
from typing import Any, AsyncGenerator, Dict, List
from opensearchpy import AsyncOpenSearch
from opensearchpy.helpers import async_bulk, BulkIndexError
from opensearchpy.exceptions import RequestError
from followthemoney import model
from followthemoney.exc import FollowTheMoneyException
from followthemoney.types.date import DateType
from followthemoney.types.name import NameType
from nomenklatura.util import fingerprint_name

from yente import settings
from yente.logs import get_logger
from yente.data.entity import Entity
from yente.data.dataset import Dataset
from yente.data import get_catalog
from yente.data.loader import load_json_lines
from yente.search.base import get_es, close_es, index_semaphore
from yente.search.mapping import make_entity_mapping
from yente.search.mapping import INDEX_SETTINGS
from yente.search.mapping import NAMES_FIELD, SOUNDEX_FIELD, NAME_PART_FIELD
from yente.data.util import expand_dates, soundex_names

log = get_logger(__name__)


async def iter_entity_docs(
    dataset: Dataset, index: str
) -> AsyncGenerator[Dict[str, Any], None]:
    if dataset.entities_url is None:
        return
    datasets = set(dataset.dataset_names)
    idx = 0
    async for data in load_json_lines(dataset.entities_url, index):
        if idx % 1000 == 0 and idx > 0:
            log.info("Index: %d entities..." % idx, index=index)
        idx += 1

        try:
            entity = Entity.from_dict(model, data)
            entity.datasets = entity.datasets.intersection(datasets)
            if not len(entity.datasets):
                entity.datasets.add(dataset.name)
            if dataset.ns is not None:
                entity = dataset.ns.apply(entity)

            texts = entity.pop("indexText")
            doc = entity.to_full_dict(matchable=True)
            doc["text"] = texts
            names = doc.pop(NameType.group, [])
            expanded_names = set(names)
            name_parts = set()
            for name in names:
                fp = fingerprint_name(name)
                if fp is not None:
                    expanded_names.add(fp)
                    name_parts.update(fp.split(WS))
            doc[NAMES_FIELD] = list(expanded_names)
            doc[NAME_PART_FIELD] = list(name_parts)
            doc[SOUNDEX_FIELD] = soundex_names(names)
            doc[DateType.group] = expand_dates(doc.pop(DateType.group, []))
            entity_id = doc.pop("id")
            yield {"_index": index, "_id": entity_id, "_source": doc}
        except FollowTheMoneyException as exc:
            log.warning("Invalid entity: %s" % exc, data=data)


async def index_entities_rate_limit(
    es: AsyncOpenSearch, dataset: Dataset, force: bool
) -> bool:
    async with index_semaphore:
        return await index_entities(es, dataset, force=force)


async def index_entities(es: AsyncOpenSearch, dataset: Dataset, force: bool) -> bool:
    """Index entities in a particular dataset, with versioning of the index."""
    if not dataset.load:
        log.debug("Dataset is going to be loaded", dataset=dataset.name)
        return False
    if dataset.entities_url is None:
        log.warning("Cannot identify resource with FtM entities", dataset=dataset.name)
        return False

    # Versioning defaults to the software version instead of a data update date:
    version = f"{settings.INDEX_VERSION}{dataset.version}"
    log.info(
        "Indexing entities",
        dataset=dataset.name,
        url=dataset.entities_url,
        version=version,
    )
    dataset_prefix = f"{settings.ENTITY_INDEX}-{dataset.name}"
    next_index = f"{dataset_prefix}-{version}"
    exists = await es.indices.exists_alias(name=settings.ENTITY_INDEX, index=next_index)
    if exists and not force:
        log.info("Index is up to date.", index=next_index)
        return False

    # await es.indices.delete(index=next_index)
    log.info("Create index", index=next_index)
    try:
        schemata = list(model.schemata.values())
        mapping = make_entity_mapping(schemata)
        body = { "mappings": mapping, "settings": INDEX_SETTINGS }
        await es.indices.create(
            index=next_index,
            body=body,
        )
    except RequestError as exc:
        log.warning(
            "Cannot create index: %s" % exc.error,
            index=next_index,
        )

    try:
        docs = iter_entity_docs(dataset, next_index)
        await async_bulk(es, docs, yield_ok=False, stats_only=True, chunk_size=1000)
    except (
        BulkIndexError,
        KeyboardInterrupt,
        OSError,
        Exception,
        asyncio.TimeoutError,
        asyncio.CancelledError,
    ) as exc:
        errors = None
        if hasattr(exc, "errors"):
            errors = exc.errors
        log.exception(
            "Indexing error: %r" % exc,
            dataset=dataset.name,
            index=next_index,
            errors=errors,
            entities_url=dataset.entities_url,
        )
        await es.indices.delete(index=next_index)
        return False

    await es.indices.refresh(index=next_index)
    await es.indices.put_alias(index=next_index, name=settings.ENTITY_INDEX)

    log.info("Index is now aliased to: %s" % settings.ENTITY_INDEX, index=next_index)
    indices: Any = await es.cat.indices(format="json")
    current: List[str] = [s.get("index") for s in indices]
    current = [c for c in current if c.startswith(f"{dataset_prefix}-")]
    if len(current) == 0:
        log.error("No index was created", index=next_index)
        return False
    for index in current:
        if index != next_index:
            log.info("Delete other index", index=index)
            await es.indices.delete(index=index)

    return True


async def update_index(force: bool = False) -> bool:
    """Reindex all datasets if there is a new version of their data contenst available,
    return boolean to indicate if the index was changed for any of them."""
    es_ = await get_es()
    es = es_
    try:
        catalog = await get_catalog()
        log.info("Index update check")
        indexers = []
        for dataset in catalog.datasets:
            indexers.append(index_entities_rate_limit(es, dataset, force))
        results = await asyncio.gather(*indexers)
        changed = True in results
        log.info("Index update complete.", changed=changed)
        return changed
    finally:
        await es.close()
        await close_es()


def update_index_threaded(force: bool = False) -> None:
    async def update_in_thread() -> None:
        try:
            await update_index(force=force)
        except (Exception, KeyboardInterrupt) as exc:
            log.exception("Index update error: %s" % exc)

    thread = threading.Thread(
        target=asyncio.run,
        args=(update_in_thread(),),
        daemon=True,
    )
    thread.start()
    # asyncio.to_thread(update_index, force=force)
