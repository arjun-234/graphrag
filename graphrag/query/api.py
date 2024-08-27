# Copyright (c) 2024 Microsoft Corporation.
# Licensed under the MIT License

"""
Query Engine API.

This API provides access to the query engine of graphrag, allowing external applications
to hook into graphrag and run queries over a knowledge graph generated by graphrag.

Contains the following functions:
 - global_search: Perform a global search.
 - global_search_streaming: Perform a global search and stream results back.
 - local_search: Perform a local search.
 - local_search_streaming: Perform a local search and stream results back.

WARNING: This API is under development and may undergo changes in future releases.
Backwards compatibility is not guaranteed at this time.
"""

from collections.abc import AsyncGenerator
from pathlib import Path
from typing import Any

import pandas as pd
from pydantic import validate_call

from graphrag.config.models.graph_rag_config import GraphRagConfig
from graphrag.config.resolve_timestamp_path import resolve_timestamp_path
from graphrag.index.progress.types import PrintProgressReporter
from graphrag.model.entity import Entity
from graphrag.query.structured_search.base import SearchResult  # noqa: TCH001
from graphrag.vector_stores.lancedb import LanceDBVectorStore
from graphrag.vector_stores.typing import VectorStoreFactory, VectorStoreType

from .factories import get_global_search_engine, get_local_search_engine
from .indexer_adapters import (
    read_indexer_covariates,
    read_indexer_entities,
    read_indexer_relationships,
    read_indexer_reports,
    read_indexer_text_units,
)
from .input.loaders.dfs import store_entity_semantic_embeddings

reporter = PrintProgressReporter("")


@validate_call(config={"arbitrary_types_allowed": True})
async def global_search(
    config: GraphRagConfig,
    nodes: pd.DataFrame,
    entities: pd.DataFrame,
    community_reports: pd.DataFrame,
    community_level: int,
    response_type: str,
    query: str,
) -> tuple[
    str | dict[str, Any] | list[dict[str, Any]],
    str | list[pd.DataFrame] | dict[str, pd.DataFrame],
]:
    """Perform a global search and return the context data and response.

    Parameters
    ----------
    - config (GraphRagConfig): A graphrag configuration (from settings.yaml)
    - nodes (pd.DataFrame): A DataFrame containing the final nodes (from create_final_nodes.parquet)
    - entities (pd.DataFrame): A DataFrame containing the final entities (from create_final_entities.parquet)
    - community_reports (pd.DataFrame): A DataFrame containing the final community reports (from create_final_community_reports.parquet)
    - community_level (int): The community level to search at.
    - response_type (str): The type of response to return.
    - query (str): The user query to search for.

    Returns
    -------
    TODO: Document the search response type and format.

    Raises
    ------
    TODO: Document any exceptions to expect.
    """
    reports = read_indexer_reports(community_reports, nodes, community_level)
    _entities = read_indexer_entities(nodes, entities, community_level)
    search_engine = get_global_search_engine(
        config,
        reports=reports,
        entities=_entities,
        response_type=response_type,
    )
    result: SearchResult = await search_engine.asearch(query=query)
    response = result.response
    context_data = _reformat_context_data(result.context_data)  # type: ignore
    return response, context_data


@validate_call(config={"arbitrary_types_allowed": True})
async def global_search_streaming(
    config: GraphRagConfig,
    nodes: pd.DataFrame,
    entities: pd.DataFrame,
    community_reports: pd.DataFrame,
    community_level: int,
    response_type: str,
    query: str,
) -> AsyncGenerator:
    """Perform a global search and return the context data and response via a generator.

    Context data is returned as a dictionary of lists, with one list entry for each record.

    Parameters
    ----------
    - config (GraphRagConfig): A graphrag configuration (from settings.yaml)
    - nodes (pd.DataFrame): A DataFrame containing the final nodes (from create_final_nodes.parquet)
    - entities (pd.DataFrame): A DataFrame containing the final entities (from create_final_entities.parquet)
    - community_reports (pd.DataFrame): A DataFrame containing the final community reports (from create_final_community_reports.parquet)
    - community_level (int): The community level to search at.
    - response_type (str): The type of response to return.
    - query (str): The user query to search for.

    Returns
    -------
    TODO: Document the search response type and format.

    Raises
    ------
    TODO: Document any exceptions to expect.
    """
    reports = read_indexer_reports(community_reports, nodes, community_level)
    _entities = read_indexer_entities(nodes, entities, community_level)
    search_engine = get_global_search_engine(
        config,
        reports=reports,
        entities=_entities,
        response_type=response_type,
    )
    search_result = search_engine.astream_search(query=query)

    # when streaming results, a context data object is returned as the first result
    # and the query response in subsequent tokens
    context_data = None
    get_context_data = True
    async for stream_chunk in search_result:
        if get_context_data:
            context_data = _reformat_context_data(stream_chunk)  # type: ignore
            yield context_data
            get_context_data = False
        else:
            yield stream_chunk


@validate_call(config={"arbitrary_types_allowed": True})
async def local_search(
    root_dir: str | None,
    config: GraphRagConfig,
    nodes: pd.DataFrame,
    entities: pd.DataFrame,
    community_reports: pd.DataFrame,
    text_units: pd.DataFrame,
    relationships: pd.DataFrame,
    covariates: pd.DataFrame | None,
    community_level: int,
    response_type: str,
    query: str,
) -> tuple[
    str | dict[str, Any] | list[dict[str, Any]],
    str | list[pd.DataFrame] | dict[str, pd.DataFrame],
]:
    """Perform a local search and return the context data and response.

    Parameters
    ----------
    - config (GraphRagConfig): A graphrag configuration (from settings.yaml)
    - nodes (pd.DataFrame): A DataFrame containing the final nodes (from create_final_nodes.parquet)
    - entities (pd.DataFrame): A DataFrame containing the final entities (from create_final_entities.parquet)
    - community_reports (pd.DataFrame): A DataFrame containing the final community reports (from create_final_community_reports.parquet)
    - text_units (pd.DataFrame): A DataFrame containing the final text units (from create_final_text_units.parquet)
    - relationships (pd.DataFrame): A DataFrame containing the final relationships (from create_final_relationships.parquet)
    - covariates (pd.DataFrame): A DataFrame containing the final covariates (from create_final_covariates.parquet)
    - community_level (int): The community level to search at.
    - response_type (str): The response type to return.
    - query (str): The user query to search for.

    Returns
    -------
    TODO: Document the search response type and format.

    Raises
    ------
    TODO: Document any exceptions to expect.
    """
    vector_store_args = (
        config.embeddings.vector_store if config.embeddings.vector_store else {}
    )
    reporter.info(f"Vector Store Args: {vector_store_args}")

    vector_store_type = vector_store_args.get("type", VectorStoreType.LanceDB)

    _entities = read_indexer_entities(nodes, entities, community_level)

    base_dir = Path(str(root_dir)) / config.storage.base_dir
    resolved_base_dir = resolve_timestamp_path(base_dir)
    lancedb_dir = resolved_base_dir / "lancedb"
    vector_store_args.update({"db_uri": str(lancedb_dir)})
    description_embedding_store = _get_embedding_description_store(
        entities=_entities,
        vector_store_type=vector_store_type,
        config_args=vector_store_args,
    )

    _covariates = read_indexer_covariates(covariates) if covariates is not None else []

    search_engine = get_local_search_engine(
        config=config,
        reports=read_indexer_reports(community_reports, nodes, community_level),
        text_units=read_indexer_text_units(text_units),
        entities=_entities,
        relationships=read_indexer_relationships(relationships),
        covariates={"claims": _covariates},
        description_embedding_store=description_embedding_store,
        response_type=response_type,
    )

    result: SearchResult = await search_engine.asearch(query=query)
    response = result.response
    context_data = _reformat_context_data(result.context_data)  # type: ignore
    return response, context_data


@validate_call(config={"arbitrary_types_allowed": True})
async def local_search_streaming(
    root_dir: str | None,
    config: GraphRagConfig,
    nodes: pd.DataFrame,
    entities: pd.DataFrame,
    community_reports: pd.DataFrame,
    text_units: pd.DataFrame,
    relationships: pd.DataFrame,
    covariates: pd.DataFrame | None,
    community_level: int,
    response_type: str,
    query: str,
) -> AsyncGenerator:
    """Perform a local search and return the context data and response via a generator.

    Parameters
    ----------
    - config (GraphRagConfig): A graphrag configuration (from settings.yaml)
    - nodes (pd.DataFrame): A DataFrame containing the final nodes (from create_final_nodes.parquet)
    - entities (pd.DataFrame): A DataFrame containing the final entities (from create_final_entities.parquet)
    - community_reports (pd.DataFrame): A DataFrame containing the final community reports (from create_final_community_reports.parquet)
    - text_units (pd.DataFrame): A DataFrame containing the final text units (from create_final_text_units.parquet)
    - relationships (pd.DataFrame): A DataFrame containing the final relationships (from create_final_relationships.parquet)
    - covariates (pd.DataFrame): A DataFrame containing the final covariates (from create_final_covariates.parquet)
    - community_level (int): The community level to search at.
    - response_type (str): The response type to return.
    - query (str): The user query to search for.

    Returns
    -------
    TODO: Document the search response type and format.

    Raises
    ------
    TODO: Document any exceptions to expect.
    """
    vector_store_args = (
        config.embeddings.vector_store if config.embeddings.vector_store else {}
    )
    reporter.info(f"Vector Store Args: {vector_store_args}")

    vector_store_type = vector_store_args.get("type", VectorStoreType.LanceDB)

    _entities = read_indexer_entities(nodes, entities, community_level)

    base_dir = Path(str(root_dir)) / config.storage.base_dir
    resolved_base_dir = resolve_timestamp_path(base_dir)
    lancedb_dir = resolved_base_dir / "lancedb"
    vector_store_args.update({"db_uri": str(lancedb_dir)})
    description_embedding_store = _get_embedding_description_store(
        entities=_entities,
        vector_store_type=vector_store_type,
        config_args=vector_store_args,
    )

    _covariates = read_indexer_covariates(covariates) if covariates is not None else []

    search_engine = get_local_search_engine(
        config=config,
        reports=read_indexer_reports(community_reports, nodes, community_level),
        text_units=read_indexer_text_units(text_units),
        entities=_entities,
        relationships=read_indexer_relationships(relationships),
        covariates={"claims": _covariates},
        description_embedding_store=description_embedding_store,
        response_type=response_type,
    )
    search_result = search_engine.astream_search(query=query)

    # when streaming results, a context data object is returned as the first result
    # and the query response in subsequent tokens
    context_data = None
    get_context_data = True
    async for stream_chunk in search_result:
        if get_context_data:
            context_data = _reformat_context_data(stream_chunk)  # type: ignore
            yield context_data
            get_context_data = False
        else:
            yield stream_chunk


def _get_embedding_description_store(
    entities: list[Entity],
    vector_store_type: str = VectorStoreType.LanceDB,
    config_args: dict | None = None,
):
    """Get the embedding description store."""
    if not config_args:
        config_args = {}

    collection_name = config_args.get(
        "query_collection_name", "entity_description_embeddings"
    )
    config_args.update({"collection_name": collection_name})
    description_embedding_store = VectorStoreFactory.get_vector_store(
        vector_store_type=vector_store_type, kwargs=config_args
    )

    description_embedding_store.connect(**config_args)

    if config_args.get("overwrite", True):
        # this step assumes the embeddings were originally stored in a file rather
        # than a vector database

        # dump embeddings from the entities list to the description_embedding_store
        store_entity_semantic_embeddings(
            entities=entities, vectorstore=description_embedding_store
        )
    else:
        # load description embeddings to an in-memory lancedb vectorstore
        # and connect to a remote db, specify url and port values.
        description_embedding_store = LanceDBVectorStore(
            collection_name=collection_name
        )
        description_embedding_store.connect(
            db_uri=config_args.get("db_uri", "./lancedb")
        )

        # load data from an existing table
        description_embedding_store.document_collection = (
            description_embedding_store.db_connection.open_table(
                description_embedding_store.collection_name
            )
        )

    return description_embedding_store


def _reformat_context_data(context_data: dict) -> dict:
    """
    Reformats context_data for all query responses.

    Reformats a dictionary of dataframes into a dictionary of lists.
    One list entry for each record. Records are grouped by original
    dictionary keys.

    Note: depending on which query algorithm is used, the context_data may not
          contain the same information (keys). In this case, the default behavior will be to
          set these keys as empty lists to preserve a standard output format.
    """
    final_format = {
        "reports": [],
        "entities": [],
        "relationships": [],
        "claims": [],
        "sources": [],
    }
    for key in context_data:
        records = context_data[key].to_dict(orient="records")
        if len(records) < 1:
            continue
        final_format[key] = records
    return final_format
