import os
import threading
import tempfile
import wave
from faster_whisper import WhisperModel

class WhisperService:
    def __init__(self, model_name=None, model_dir="models/whisper-small"):
        self.requested_model = model_name or os.getenv('WHISPER_MODEL', '').strip()
        self.gpu_model = os.getenv('WHISPER_GPU_MODEL', 'small').strip() or 'small'
        self.cpu_model = os.getenv('WHISPER_CPU_MODEL', 'base.en').strip() or 'base.en'
        self.model_name = self.requested_model or self.gpu_model
        self.language = os.getenv('WHISPER_LANGUAGE', 'en').strip() or 'en'
        try:
            configured_threads = int(os.getenv('WHISPER_CPU_THREADS', '4'))
        except (TypeError, ValueError):
            configured_threads = 4
        self.cpu_threads = max(1, min(configured_threads, os.cpu_count() or 1))
        root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        self.model_dir = model_dir if os.path.isabs(model_dir) else os.path.join(root, model_dir)
        self.model = None
        self.is_downloading = False
        self.is_ready = False
        self.device = "cpu"
        self.compute_type = "int8"
        self.error = None
        self.lock = threading.Lock()
        self.ready_event = threading.Event()
        self.warmed = False

    def initialize(self):
        with self.lock:
            if self.is_ready or self.is_downloading:
                return
            self.is_downloading = True

        thread = threading.Thread(target=self._load_model_background, daemon=True)
        thread.start()

    def _load_model_background(self):
        try:
            # Check for CUDA availability
            try:
                import ctranslate2
                if ctranslate2.get_cuda_device_count() > 0:
                    self.device = "cuda"
                    self.compute_type = "float16"
            except Exception:
                pass

            self.model_name = self.requested_model or (
                self.gpu_model if self.device == 'cuda' else self.cpu_model
            )

            print(f"[Whisper] model: {self.model_name}")
            print(f"[Whisper] device: {self.device}")
            print(f"[Whisper] loading...")

            os.makedirs(self.model_dir, exist_ok=True)
            try:
                self.model = WhisperModel(
                    self.model_name,
                    device=self.device,
                    compute_type=self.compute_type,
                    download_root=self.model_dir,
                    cpu_threads=self.cpu_threads,
                )
            except Exception:
                if self.device != 'cuda':
                    raise
                self.device, self.compute_type = 'cpu', 'int8'
                self.model_name = self.requested_model or self.cpu_model
                self.model = WhisperModel(
                    self.model_name,
                    device=self.device,
                    compute_type=self.compute_type,
                    download_root=self.model_dir,
                    cpu_threads=self.cpu_threads,
                )
            self._warm_model()
            self.is_ready = True
            self.ready_event.set()
            print("[Whisper] ready")
        except Exception as e:
            print(f"[Whisper] Error loading model: {e}")
            self.error = str(e)
            self.ready_event.set()
        finally:
            self.is_downloading = False

    def get_status(self):
        return {
            "available": self.is_ready,
            "model": self.model_name,
            "device": self.device,
            "compute_type": self.compute_type,
            "cpu_threads": self.cpu_threads,
            "language": self.language,
            "loaded": self.is_ready,
            "downloading": self.is_downloading,
            "warmed": self.warmed,
            "error": self.error
        }

    def transcribe(self, audio_path):
        # Do not block an interactive voice request while the model is still
        # downloading/warming. The browser client has a speech-recognition
        # fallback and should switch to it immediately instead of appearing
        # frozen for up to 20 seconds.
        if not self.is_ready:
            if self.is_downloading:
                raise Exception("whisper_model_loading")
            if self.error:
                raise Exception(f"whisper_model_unavailable: {self.error}")
            raise Exception("whisper_model_unavailable")

        # We use Vad filtering
        segments, info = self.model.transcribe(
            audio_path,
            vad_filter=True,
            beam_size=1,
            condition_on_previous_text=False,
            language=self.language,
        )

        text = " ".join([segment.text for segment in segments])
        return {
            "text": text.strip(),
            "language": info.language,
            "duration": info.duration
        }

    def _warm_model(self):
        path = None
        try:
            with tempfile.NamedTemporaryFile(suffix='.wav', delete=False) as handle:
                path = handle.name
            with wave.open(path, 'wb') as output:
                output.setnchannels(1)
                output.setsampwidth(2)
                output.setframerate(16000)
                output.writeframes(b'\x00\x00' * 3200)
            segments, _info = self.model.transcribe(
                path,
                beam_size=1,
                condition_on_previous_text=False,
                language=self.language,
            )
            list(segments)
            # Production transcription enables VAD. Exercise that path during
            # startup too so Silero and its kernels are not loaded on first use.
            vad_segments, _vad_info = self.model.transcribe(
                path,
                vad_filter=True,
                beam_size=1,
                condition_on_previous_text=False,
                language=self.language,
            )
            list(vad_segments)
            self.warmed = True
        except Exception as exc:
            print(f"[Whisper] warm-up skipped: {exc}")
        finally:
            if path and os.path.exists(path):
                os.remove(path)

whisper_service = WhisperService()