"""Annonces vocales avec la synthèse vocale intégrée à Windows (rien à installer).

Un seul processus PowerShell reste ouvert et lit les phrases qu'on lui envoie : pas de délai
de démarrage à chaque annonce. Hors Windows (démo, tests), les phrases sont affichées dans la console.
"""
import queue
import subprocess
import sys
import threading

POWERSHELL_SCRIPT = r"""
[Console]::InputEncoding = [System.Text.Encoding]::UTF8
Add-Type -AssemblyName System.Speech
$s = New-Object System.Speech.Synthesis.SpeechSynthesizer
$fr = $s.GetInstalledVoices() | Where-Object { $_.VoiceInfo.Culture.Name -like 'fr*' } | Select-Object -First 1
if ($fr) { $s.SelectVoice($fr.VoiceInfo.Name) }
$s.Rate = 0
$s.Volume = 100
while (($line = [Console]::In.ReadLine()) -ne $null) {
    if ($line.StartsWith('#RATE ')) { $s.Rate = [int]$line.Substring(6) }
    elseif ($line.StartsWith('#VOLUME ')) { $s.Volume = [int]$line.Substring(8) }
    elseif ($line) { $s.Speak($line) }
}
"""

# débit de parole : de -10 (très lent) à 10 (très rapide) pour la synthèse vocale de Windows
RATES = {"lent": -2, "normal": 0, "rapide": 2}


class Speaker:
    def __init__(self):
        self.queue = queue.Queue(maxsize=5)  # si ça s'accumule, on jette plutôt que parler en retard
        self.process = None
        self.history = []  # dernières annonces (affichées dans l'interface)
        self.rate, self.volume = 0, 100
        threading.Thread(target=self._run, daemon=True, name="voice").start()

    def configure(self, rate=None, volume=None):
        """Débit (« lent », « normal », « rapide ») et volume (0 à 100) de la voix."""
        if rate is not None:
            self.rate = RATES.get(rate, 0)
        if volume is not None:
            self.volume = max(0, min(100, int(volume)))
        self._send_settings = True

    def say(self, text):
        self.history = (self.history + [text])[-20:]
        try:
            self.queue.put_nowait(text)
        except queue.Full:
            pass

    def _start(self):
        if sys.platform != "win32":
            return None
        try:
            return subprocess.Popen(
                ["powershell", "-NoProfile", "-NonInteractive", "-Command", POWERSHELL_SCRIPT],
                stdin=subprocess.PIPE, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        except OSError as exc:
            print(f"[voix] synthèse vocale indisponible : {exc!r}")
            return None

    def _run(self):
        while True:
            text = self.queue.get()
            if self.process is None or self.process.poll() is not None:
                self.process = self._start()
                self._send_settings = True  # nouveau processus : il faut lui redonner les réglages
            if self.process is None:
                print(f"[voix] {text}")
                continue
            try:
                if getattr(self, "_send_settings", False):
                    self.process.stdin.write(f"#RATE {self.rate}\n#VOLUME {self.volume}\n".encode("utf-8"))
                    self._send_settings = False
                self.process.stdin.write((text.replace("\n", " ") + "\n").encode("utf-8"))
                self.process.stdin.flush()
            except OSError:
                self.process = None


speaker = Speaker()


# --- formulations faciles à prononcer -------------------------------------------------------

def spoken_time(seconds):
    """82.43 -> '1 22 4' (minutes, secondes, dixièmes), comme les ingénieurs de course."""
    tenths = round(seconds * 10)
    minutes, rest = divmod(tenths, 600)
    secs, tenth = divmod(rest, 10)
    return f"{minutes} {secs:02d} {tenth}" if minutes else f"{secs} {tenth}"


def spoken_delta(delta):
    """-0.34 -> 'moins 0 virgule 3' ; 1.25 -> 'plus 1 virgule 3' ; 0.04 -> 'plus 4 centièmes'."""
    sign = "moins" if delta < 0 else "plus"
    if abs(delta) < 0.095:  # sous le dixième : on parle en centièmes
        hundredths = max(1, round(abs(delta) * 100))
        return f"{sign} {hundredths} centième{'s' if hundredths > 1 else ''}"
    whole, tenth = divmod(round(abs(delta) * 10), 10)
    return f"{sign} {whole} virgule {tenth}"
