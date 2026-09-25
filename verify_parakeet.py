"""Fast Together/Parakeet setup check. Does not process a VOD."""
from config import Config
from parakeet_transcribe import ParakeetTranscriber

def main():
    print("[verify] checking Together AI Parakeet credentials/model...")
    provider = ParakeetTranscriber(Config)
    provider.probe()
    print("[verify] PASS - Together Parakeet is reachable and authorized.")
    print(f"[verify] model={Config.PARAKEET_MODEL}")
    print(f"[verify] concurrency={Config.PARAKEET_CONCURRENCY}")
    print(f"[verify] VOD chunk={Config.PARAKEET_CHUNK_SECONDS:.0f}s overlap={Config.PARAKEET_CHUNK_OVERLAP_SECONDS:.0f}s")
    print("[verify] VOD logs must show [asr] ACTIVE provider=Together/Parakeet and [parakeet] OK.")

if __name__ == "__main__":
    main()
