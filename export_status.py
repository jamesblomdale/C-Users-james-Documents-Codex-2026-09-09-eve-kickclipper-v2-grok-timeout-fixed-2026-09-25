def finish_exports(succeeded, attempted):
    failed = attempted - succeeded
    print(f"[vod] export summary: {succeeded} clips produced, {failed} failed ({attempted} attempted)")
    if failed:
        raise RuntimeError(f"Export incomplete: {failed}/{attempted} clips failed. Saved source segments and captions were retained for retry.")
    print("PROGRESS done 100")
