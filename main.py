import sys

sys.path.append("../../")

from mindexer.utils.sampling import SampleEstimator
from mindexer.utils.mongodb import MongoCollection
from mindexer.utils.query import Query
from mindexer.utils.workload import Workload

from pymongo import MongoClient
from itertools import permutations
import pandas as pd
import numpy as np

import time
import argparse

SYSTEM_PROFILE = "system.profile"
DEFAULT_SAMPLE_DB = "mindexer_samples"
DEFAULT_SAMPLE_RATIO = 0.001

MAX_INDEX_FIELDS = 3

IXSCAN_COST = 0.5
FETCH_COST = 10
SORT_COST = 10


def main(args):
    namespace = f"{args.db}.{args.collection}"
    collection = MongoCollection(args.uri, args.db, args.collection)
    database = collection.db

    # -- Workload
    print(f"\n>> scanning system.profile collection for queries on {namespace}\n")
    # find all queries in system.profile related to the collection
    profile_collection = database[SYSTEM_PROFILE]

    # TODO include aggregate commands as well
    profile_docs = [
        doc["command"]
        for doc in profile_collection.find({"ns": namespace, "op": "query"})
    ]

    # extract MQL queries
    workload = Workload()

    for doc in profile_docs:
        try:
            query = Query.from_mql(doc["filter"])
            if "limit" in doc:
                query.limit = doc["limit"]
            if "sort" in doc:
                query.sort = tuple(doc["sort"].keys())
            if "projection" in doc:
                query.projection = tuple(doc["projection"].keys())

            workload.queries.append(query)
        except AssertionError as e:
            print(f"    Warning: skipping query {doc['filter']}. {e}")

    print(f"\n>> found {len(workload)} queries for namespace {namespace}\n")
    if args.verbose:
        workload.print()

    # -- Sample Estimator

    estimator = SampleEstimator(
        collection,
        sample_ratio=args.sample_ratio,
        sample_db_name=args.sample_db,
        persist=True,
    )

    print(
        f"\n>> extracted data sample, persisted at {args.sample_db}.{args.collection}\n"
    )

    # -- generate list of index candidates

    candidates = set()
    for query in workload:
        num_preds = len(query.fields)
        # only consider indexes with at most MAX_INDEX_FIELDS fields
        for i in range(min(num_preds, MAX_INDEX_FIELDS)):
            for candidate in permutations(query.fields, i + 1):
                candidates.add(candidate)

    print(f"\n>> generated {len(candidates)} candidate indexes\n")
    if args.verbose:
        for ic, candidate in enumerate(candidates):
            print(f"    {ic}   {candidate}")

    # -- score index candidates
    estimate_cache = {}

    def get_estimate(query):
        if query in estimate_cache:
            return estimate_cache[query]

        # -- estimated cardinalities with model
        est = estimator.estimate(query)

        estimate_cache[query] = est
        return est

    score_time = time.time()

    print("\n>> evaluating scores for index candidates\n")
    scores = pd.DataFrame(
        0,
        index=range(len(workload)),
        columns=list(candidates),
    )

    for nq, query in enumerate(workload):
        print(f"    query #{nq:<2}: {query}")
        for nc, candidate in enumerate(candidates):

            # score index for filtering
            fetch_query = query.index_intersect(candidate)
            if len(fetch_query.predicates) == 0:
                # index can't be used, no benefit over collection scan
                benefit = 0
            else:
                # different costs per unit of work depending if the query is covered or not
                if query.is_covered(candidate):
                    cost = IXSCAN_COST
                else:
                    cost = FETCH_COST

                # estimate for number of "work units"
                est = get_estimate(fetch_query)

                # if the query has a limit, and all fields of the filter are in the
                # index, then we can cap the upper bound of the estimate at limit.
                # if not, then the expected number of units of work to find all matches
                # is equal to est. The expected case is equal to the worst case.
                # See https://math.stackexchange.com/questions/2595408/hypergeometric-distribution-expected-number-of-draws-until-k-successes-are-dra
                if query.limit is not None and query.is_subset(candidate):
                    est = min(query.limit, est)

                # benefit of the index over a collection scan (assuming cost of
                # collscan = 1.0 relative to other costs)
                benefit = estimator.get_cardinality() * 1.0 - est * cost

            # add additional benefit points if index can be used for sorting
            if query.can_use_sort(candidate):
                # cap at 1 to avoid log2(0), which is undefined
                est = max(1, get_estimate(query))
                benefit += est * np.log2(est) * SORT_COST

            scores.iat[nq, nc] = benefit

    score_duration_ms = (time.time() - score_time) * 1000
    if args.verbose:
        print(f"   took {score_duration_ms} ms.\n")

    def printScoreTable(scores):
        print("score table (rows=queries, columns=index candidates)")
        print(
            scores.rename(
                lambda c: list(candidates).index(c), axis="columns"
            ).reset_index(drop=True)
        )

    if args.verbose:
        printScoreTable(scores)

    # -- select indexes greedily

    estimator_indexes = []

    idx_scores = scores.copy()
    for i in range(len(candidates)):

        # if nothing can be improved, we're done
        if (idx_scores <= 0).values.all():
            break

        # --- sum scores of all queries
        topscore = idx_scores.sum(axis=0).sort_values(ascending=False)
        idx = topscore.index[0]

        # remove index from the score table
        idx_scores.drop(idx, axis="columns", inplace=True)

        estimator_indexes.append(idx)

        ### update score matrix
        # for each query (row) and all created indexes (columns)
        # pick the maximum non-zero number and subtract from
        # the current score.
        for qi, query in enumerate(workload):
            # scores of existing indexes that can support this query (no 0s)
            # TODO: DeprecationWarning !!
            existing_scores = [
                s for s in scores[estimator_indexes].iloc[qi].tolist() if s != 0
            ]
            if len(existing_scores) == 0:
                # if no existing index can support this query, the current score remains
                continue
            best_existing = max(existing_scores)

            # new score is the difference between the best index so far and this index
            columns = idx_scores.columns
            idx_scores.loc[qi, columns] = scores.loc[qi, columns].subtract(
                best_existing, axis=0
            )

            # since an existing index exists for this query, we can't make it worse:
            # set negative values for this row (=query) to 0.
            idx_scores.iloc[qi].mask(idx_scores.iloc[qi] < 0, 0, inplace=True)

    print(f"\n>> dropping sample collection {args.sample_db}.{args.collection}\n")
    estimator.drop_sample()

    print(f"\n>> recommending {len(estimator_indexes)} index(es)\n")
    for idx in estimator_indexes:
        print("    ", dict(zip(idx, [1] * len(idx))))


if __name__ == "__main__":
    # Instantiate the CLI argument parser
    parser = argparse.ArgumentParser(
        description="Experimental Index Recommendation Tool for MongoDB."
    )

    # URI, database and collection arguments
    parser.add_argument(
        "--uri",
        type=str,
        metavar="<uri>",
        help="mongodb uri connection string",
        required=True,
    )
    parser.add_argument(
        "-d", "--db", metavar="<db>", type=str, help="database name", required=True
    )
    parser.add_argument(
        "-c",
        "--collection",
        type=str,
        metavar="<coll>",
        help="collection name",
        required=True,
    )
    parser.add_argument(
        "--sample-ratio", type=float, default=0.001, help="sample ratio (default=0.001)"
    )
    parser.add_argument(
        "--sample-db",
        type=str,
        default=DEFAULT_SAMPLE_DB,
        help="sample database name (default=mindexer_samples)",
    )
    parser.add_argument("-v", "--verbose", action="store_true", help="verbose output")

    args = parser.parse_args()
    main(args)
