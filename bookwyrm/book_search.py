""" using a bookwyrm instance as a source of book data """
from functools import reduce
import operator

from django.contrib.postgres.search import SearchRank, SearchQuery
from django.db.models import OuterRef, Subquery, F, Q

from bookwyrm import models
from bookwyrm.connectors.abstract_connector import SearchResult
from bookwyrm.settings import MEDIA_FULL_URL


# pylint: disable=arguments-differ
def search(query, min_confidence=0, filters=None):
    """search your local database"""
    filters = filters or []
    if not query:
        return []
    # first, try searching unqiue identifiers
    results = search_identifiers(query, *filters)
    if not results:
        # then try searching title/author
        results = search_title_author(query, min_confidence, *filters)
    return results


def isbn_search(query):
    """search your local database"""
    if not query:
        return []

    filters = [{f: query} for f in ["isbn_10", "isbn_13"]]
    results = models.Edition.objects.filter(
        reduce(operator.or_, (Q(**f) for f in filters))
    ).distinct()

    # when there are multiple editions of the same work, pick the default.
    # it would be odd for this to happen.

    default_editions = models.Edition.objects.filter(
        parent_work=OuterRef("parent_work")
    ).order_by("-edition_rank")
    results = (
        results.annotate(default_id=Subquery(default_editions.values("id")[:1])).filter(
            default_id=F("id")
        )
        or results
    )
    return results


def format_search_result(search_result):
    """convert a book object into a search result object"""
    cover = None
    if search_result.cover:
        cover = f"{MEDIA_FULL_URL}{search_result.cover}"

    return SearchResult(
        title=search_result.title,
        key=search_result.remote_id,
        author=search_result.author_text,
        year=search_result.published_date.year
        if search_result.published_date
        else None,
        cover=cover,
        confidence=search_result.rank if hasattr(search_result, "rank") else 1,
        connector="",
    ).json()


def search_identifiers(query, *filters):
    """tries remote_id, isbn; defined as dedupe fields on the model"""
    # pylint: disable=W0212
    or_filters = [
        {f.name: query}
        for f in models.Edition._meta.get_fields()
        if hasattr(f, "deduplication_field") and f.deduplication_field
    ]
    results = models.Edition.objects.filter(
        *filters, reduce(operator.or_, (Q(**f) for f in or_filters))
    ).distinct()
    if results.count() <= 1:
        return results

    # when there are multiple editions of the same work, pick the default.
    # it would be odd for this to happen.
    default_editions = models.Edition.objects.filter(
        parent_work=OuterRef("parent_work")
    ).order_by("-edition_rank")
    return (
        results.annotate(default_id=Subquery(default_editions.values("id")[:1])).filter(
            default_id=F("id")
        )
        or results
    )


def search_title_author(query, min_confidence, *filters):
    """searches for title and author"""
    query = SearchQuery(query, config="simple") | SearchQuery(query, config="english")
    results = (
        models.Edition.objects.filter(*filters, search_vector=query)
        .annotate(rank=SearchRank(F("search_vector"), query))
        .filter(rank__gt=min_confidence)
        .order_by("-rank")
    )

    # when there are multiple editions of the same work, pick the closest
    editions_of_work = results.values("parent_work__id").values_list("parent_work__id")

    # filter out multiple editions of the same work
    for work_id in set(editions_of_work):
        editions = results.filter(parent_work=work_id)
        default = editions.order_by("-edition_rank").first()
        default_rank = default.rank if default else 0
        # if mutliple books have the top rank, pick the default edition
        if default_rank == editions.first().rank:
            yield default
        else:
            yield editions.first()
