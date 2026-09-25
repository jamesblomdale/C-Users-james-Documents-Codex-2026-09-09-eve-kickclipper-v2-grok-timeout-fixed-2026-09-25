"""Reuse completed exports without moving accounts or clip folders."""
import json
from pathlib import Path
from pipeline_state import atomic_json, fingerprint
from retry_failed_exports import playable


class RenderCheckpoint:
    def __init__(self, folder, settings, words):
        self.folder, self.settings, self.words = folder, settings, words

    def key(self, candidate):
        return self.folder/(fingerprint({'settings':self.settings,'start':candidate.start,'end':candidate.end,
            'words':[(w.text,w.start,w.end) for w in self.words if candidate.start <= w.start <= candidate.end]})+'.json')

    def find(self, candidate):
        path = self.key(candidate)
        try:
            clip = Path(json.loads(path.read_text(encoding='utf-8'))['folder'])
            # Checkpoint is private to one account/source. Require existing complete artifacts.
            if playable(clip/'clip.mp4') and all((clip/n).is_file() for n in ('posts.txt','score.json','recipe.json')):
                print('[render] reusing completed clip from this source; existing library ownership retained',flush=True)
                return clip
        except (OSError,ValueError,KeyError):
            pass

    def save(self, candidate, folder):
        atomic_json(self.key(candidate),{'status':'COMPLETED','folder':str(folder.resolve())})
