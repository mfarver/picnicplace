"""The order a run deals with its placements in, as the Studio lists them."""

from picnicplace.job.run import ItemStatus, RunItem, processing_order


def item(index: int, nozzles: list[str], status: ItemStatus = "pending") -> RunItem:
    return RunItem(
        index=index,
        copy_id=None,
        ref=f"R{index}",
        part_id="P",
        footprint_id="F",
        x=0.0,
        y=0.0,
        rotation=0.0,
        slot_id="1",
        nozzles=nozzles,
        status=status,
    )


def test_parts_only_one_nozzle_takes_are_paired_with_the_others() -> None:
    """Items 0-2 only N1 takes, 3 and 4 only N2: each batch is one of each, so the run
    goes 0, 3, 1, 4, 2, and not in the plan's order. Skipped items come last."""
    items = [item(i, ["N1"]) for i in range(3)] + [item(3, ["N2"]), item(4, ["N2"])]
    items.append(item(5, ["N1", "N2"], "skipped"))
    assert processing_order(items, ["N1", "N2"], []) == [0, 3, 1, 4, 2, 5]


def test_items_taken_keep_their_place() -> None:
    """Taken in the order 0, 3 (now placed) and 1 (on a nozzle, still pending): they come
    first, as taken, and the rest are planned from there."""
    items = [item(i, ["N1"]) for i in range(3)] + [item(3, ["N2"]), item(4, ["N2"])]
    items[0].status = items[3].status = "placed"
    assert processing_order(items, ["N1", "N2"], [0, 3, 1]) == [0, 3, 1, 2, 4]


def test_a_run_kept_before_taken_lists_its_placed_items_first() -> None:
    """No order taken kept (a run saved before there was one): its placed items still come
    before those to go, which are planned in batches, and skipped ones last."""
    items = [item(0, ["N1"], "skipped"), item(1, ["N1"]), item(2, ["N2"], "placed")]
    items.append(item(3, ["N2"]))
    assert processing_order(items, ["N1", "N2"], []) == [2, 1, 3, 0]
