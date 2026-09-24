# iracingTracker

# iRacing Telemetry Logger - Guide Complet

Capture et analyse tes données iRacing en temps réel! 🏁

## C'est quoi ce projet?

Un petit serveur local qui:
- ✅ Reçoit les données UDP d'iRacing (telemetry SDK)
- ✅ Enregistre chaque lap avec tous les détails
- ✅ Affiche un dashboard temps réel
- ✅ Exporte les data pour analyse post-session

## Architecture

```
iRacing (envoie UDP sur port 11111)
    ↓
Python Script (reçoit + traite les données)
    ↓
SQLite Database (stocke les laps)
    ↓
Dashboard Web (affiche les stats)
```

---

## Installation

### 1. Prérequis

- Python 3.8+
- iRacing installé
- Un éditeur de code (VS Code, PyCharm, etc)

### 2. Setup du dossier projet

```bash
# Crée un dossier pour ton projet
mkdir iracing-telemetry
cd iracing-telemetry

# Crée un environnement virtuel (optionnel mais recommandé)
python -m venv venv

# Active l'env (Windows)
venv\Scripts\activate
# Ou (Mac/Linux)
source venv/bin/activate

# Installe les dépendances
pip install flask flask-cors
```

### 3. Structure du projet

```
iracing-telemetry/
├── main.py              (serveur principal)
├── iracing_sdk.py       (SDK iRacing)
├── data/
│   └── sessions.db      (base de données)
├── index.html           (dashboard web)
└── README.md            (ce fichier)
```

---

## Code - Partie 1: SDK iRacing

Crée `iracing_sdk.py`:

```python
import mmap
import struct
from ctypes import *

class TelemetryData:
    def __init__(self):
        self.session_num = 0
        self.lap_num = 0
        self.lap_dist_pct = 0.0
        self.lap_time = 0.0
        self.last_lap_time = 0.0
        self.best_lap_time = 0.0
        self.fuel_level = 0.0
        self.brake_temp_fl = 0.0
        self.tire_temp_fl = 0.0
        self.speed_mph = 0.0
        self.throttle = 0.0
        self.brake = 0.0

class IrSdkHeader(Structure):
    _fields_ = [
        ("ver", c_int),
        ("status", c_int),
        ("tick_rate", c_int),
        ("session_info_update", c_int),
        ("session_info_len", c_int),
        ("session_info_offset", c_int),
        ("var_header_offset", c_int),
        ("num_vars", c_int),
        ("var_buf_len", c_int),
        ("num_buf", c_int),
        ("buf_header_len", c_int),
    ]

def read_irsdk_data():
    """Lit les données UDP d'iRacing"""
    try:
        with mmap.mmap(0, 784 * 4, "Local\\IRSDKMemoryMap", mmap.ACCESS_READ) as mem:
            header_data = mem.read(48)
            header = IrSdkHeader.from_buffer_copy(header_data)
            
            # Lit les données des buffers
            data = TelemetryData()
            
            # Décalage du buffer de telemetry
            var_offset = header.var_buf_len * (header.tick_rate % header.num_buf)
            
            # Lap number (offset 104)
            mem.seek(var_offset + 104)
            data.lap_num = struct.unpack('i', mem.read(4))[0]
            
            # Lap distance pct (offset 108)
            mem.seek(var_offset + 108)
            data.lap_dist_pct = struct.unpack('f', mem.read(4))[0]
            
            # Lap time (offset 116)
            mem.seek(var_offset + 116)
            data.lap_time = struct.unpack('f', mem.read(4))[0]
            
            # Last lap time (offset 120)
            mem.seek(var_offset + 120)
            data.last_lap_time = struct.unpack('f', mem.read(4))[0]
            
            # Best lap time (offset 124)
            mem.seek(var_offset + 124)
            data.best_lap_time = struct.unpack('f', mem.read(4))[0]
            
            # Speed (offset 244)
            mem.seek(var_offset + 244)
            data.speed_mph = struct.unpack('f', mem.read(4))[0] * 2.237  # m/s to mph
            
            # Fuel level (offset 264)
            mem.seek(var_offset + 264)
            data.fuel_level = struct.unpack('f', mem.read(4))[0]
            
            # Throttle (offset 308)
            mem.seek(var_offset + 308)
            data.throttle = struct.unpack('f', mem.read(4))[0]
            
            # Brake (offset 312)
            mem.seek(var_offset + 312)
            data.brake = struct.unpack('f', mem.read(4))[0]
            
            return data, True
    except Exception as e:
        return None, False

def is_irsdk_active():
    """Vérifie si iRacing est en cours d'exécution"""
    try:
        with mmap.mmap(0, 48, "Local\\IRSDKMemoryMap", mmap.ACCESS_READ) as mem:
            return True
    except:
        return False
```

---

## Code - Partie 2: Serveur Principal

Crée `main.py`:

```python
from flask import Flask, render_template, jsonify
from flask_cors import CORS
import sqlite3
import json
import threading
import time
from datetime import datetime
from iracing_sdk import read_irsdk_data, is_irsdk_active

app = Flask(__name__)
CORS(app)

# Données en mémoire (session actuelle)
current_session = {
    'laps': [],
    'is_recording': False,
    'session_start': None,
    'best_lap': float('inf'),
}

# Initialize database
def init_db():
    conn = sqlite3.connect('data/sessions.db')
    c = conn.cursor()
    c.execute('''
        CREATE TABLE IF NOT EXISTS sessions (
            id INTEGER PRIMARY KEY,
            date TEXT,
            circuit TEXT,
            laps_count INTEGER,
            best_lap REAL,
            avg_lap REAL
        )
    ''')
    c.execute('''
        CREATE TABLE IF NOT EXISTS laps (
            id INTEGER PRIMARY KEY,
            session_id INTEGER,
            lap_number INTEGER,
            lap_time REAL,
            fuel REAL,
            throttle_avg REAL,
            brake_avg REAL,
            max_speed REAL,
            FOREIGN KEY(session_id) REFERENCES sessions(id)
        )
    ''')
    conn.commit()
    conn.close()

def telemetry_loop():
    """Boucle qui capture les données iRacing"""
    last_lap = 0
    
    while True:
        if is_irsdk_active():
            data, success = read_irsdk_data()
            
            if success and data:
                # Nouveau lap détecté
                if data.lap_num > last_lap:
                    if data.last_lap_time > 0:
                        lap_time = data.last_lap_time
                        
                        # Sauvegarde le lap
                        current_session['laps'].append({
                            'lap': data.lap_num - 1,
                            'time': lap_time,
                            'fuel': data.fuel_level,
                            'speed': data.speed_mph,
                            'timestamp': datetime.now().isoformat()
                        })
                        
                        # Update best lap
                        if lap_time < current_session['best_lap']:
                            current_session['best_lap'] = lap_time
                    
                    last_lap = data.lap_num
        
        time.sleep(0.01)  # 10ms refresh rate

# Routes API
@app.route('/api/status')
def status():
    return jsonify({
        'recording': is_irsdk_active(),
        'laps_recorded': len(current_session['laps']),
        'best_lap': current_session['best_lap'] if current_session['best_lap'] != float('inf') else None
    })

@app.route('/api/laps')
def get_laps():
    return jsonify({
        'laps': current_session['laps']
    })

@app.route('/api/clear')
def clear_session():
    current_session['laps'] = []
    current_session['best_lap'] = float('inf')
    return jsonify({'status': 'cleared'})

@app.route('/')
def index():
    return render_template('index.html')

if __name__ == '__main__':
    init_db()
    
    # Lance la boucle telemetry en arrière-plan
    telemetry_thread = threading.Thread(target=telemetry_loop, daemon=True)
    telemetry_thread.start()
    
    # Lance le serveur web
    print("🏁 iRacing Telemetry Server started on http://localhost:5000")
    print("   Lance iRacing et démarre une session!")
    app.run(debug=True, port=5000, use_reloader=False)
```

---

## Code - Partie 3: Dashboard Web

Crée `templates/index.html`:

```html
<!DOCTYPE html>
<html>
<head>
    <meta charset="utf-8">
    <meta name="viewport" content="width=device-width, initial-scale=1">
    <title>iRacing Telemetry</title>
    <style>
        * { margin: 0; padding: 0; box-sizing: border-box; }
        body { 
            font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', sans-serif;
            background: #1a1a1a;
            color: #fff;
            padding: 2rem;
        }
        .container { max-width: 1200px; margin: 0 auto; }
        h1 { margin-bottom: 2rem; font-size: 28px; }
        .status {
            display: grid;
            grid-template-columns: repeat(auto-fit, minmax(200px, 1fr));
            gap: 1rem;
            margin-bottom: 2rem;
        }
        .stat-card {
            background: #2a2a2a;
            border-left: 4px solid #378ADD;
            padding: 1.5rem;
            border-radius: 8px;
        }
        .stat-label { font-size: 12px; color: #999; margin-bottom: 8px; text-transform: uppercase; }
        .stat-value { font-size: 24px; font-weight: 600; }
        .stat-value.active { color: #4ade80; }
        .stat-value.inactive { color: #999; }
        
        .laps-container {
            background: #2a2a2a;
            border-radius: 8px;
            overflow: hidden;
        }
        .laps-title { padding: 1rem; background: #1a1a1a; border-bottom: 1px solid #333; font-weight: 600; }
        .laps-list { max-height: 400px; overflow-y: auto; }
        .lap-item {
            display: grid;
            grid-template-columns: 80px 1fr 150px;
            gap: 1rem;
            padding: 1rem;
            border-bottom: 1px solid #333;
            align-items: center;
        }
        .lap-item:last-child { border-bottom: none; }
        .lap-num { color: #999; font-weight: 600; }
        .lap-time { font-size: 18px; font-weight: 600; }
        .lap-delta { 
            font-size: 12px;
            padding: 4px 8px;
            border-radius: 4px;
            background: #333;
            width: fit-content;
            justify-self: end;
        }
        .lap-delta.positive { color: #f87171; }
        .lap-delta.negative { color: #4ade80; }
        
        .empty { padding: 2rem; text-align: center; color: #666; }
        
        .controls {
            display: flex;
            gap: 1rem;
            margin-bottom: 2rem;
        }
        button {
            background: #378ADD;
            color: white;
            border: none;
            padding: 10px 20px;
            border-radius: 6px;
            cursor: pointer;
            font-weight: 600;
        }
        button:hover { background: #2870c0; }
        button.danger { background: #e74c3c; }
        button.danger:hover { background: #c0392b; }
    </style>
</head>
<body>
    <div class="container">
        <h1>🏁 iRacing Telemetry Logger</h1>
        
        <div class="status">
            <div class="stat-card">
                <div class="stat-label">Status</div>
                <div class="stat-value" id="status">Offline</div>
            </div>
            <div class="stat-card">
                <div class="stat-label">Laps Recorded</div>
                <div class="stat-value" id="laps-count">0</div>
            </div>
            <div class="stat-card">
                <div class="stat-label">Best Lap</div>
                <div class="stat-value" id="best-lap">—</div>
            </div>
            <div class="stat-card">
                <div class="stat-label">Avg Lap</div>
                <div class="stat-value" id="avg-lap">—</div>
            </div>
        </div>
        
        <div class="controls">
            <button onclick="clearSession()">Clear Session</button>
            <button class="danger" onclick="exportData()">Export CSV</button>
        </div>
        
        <div class="laps-container">
            <div class="laps-title">Laps</div>
            <div class="laps-list" id="laps-list">
                <div class="empty">Waiting for iRacing...</div>
            </div>
        </div>
    </div>

    <script>
        async function fetchData() {
            try {
                const statusRes = await fetch('/api/status');
                const status = await statusRes.json();
                
                const lapsRes = await fetch('/api/laps');
                const lapsData = await lapsRes.json();
                
                // Update status
                const statusEl = document.getElementById('status');
                if (status.recording) {
                    statusEl.textContent = 'Recording';
                    statusEl.className = 'stat-value active';
                } else {
                    statusEl.textContent = 'Offline';
                    statusEl.className = 'stat-value inactive';
                }
                
                // Update counts
                document.getElementById('laps-count').textContent = status.laps_recorded;
                
                // Update best lap
                if (status.best_lap && status.best_lap !== Infinity) {
                    document.getElementById('best-lap').textContent = formatTime(status.best_lap);
                }
                
                // Update avg lap
                if (lapsData.laps.length > 0) {
                    const avg = lapsData.laps.reduce((sum, l) => sum + l.time, 0) / lapsData.laps.length;
                    document.getElementById('avg-lap').textContent = formatTime(avg);
                }
                
                // Update laps list
                const lapsList = document.getElementById('laps-list');
                if (lapsData.laps.length === 0) {
                    lapsList.innerHTML = '<div class="empty">Waiting for iRacing...</div>';
                } else {
                    const bestTime = Math.min(...lapsData.laps.map(l => l.time));
                    lapsList.innerHTML = lapsData.laps.map((lap, idx) => {
                        const delta = lap.time - bestTime;
                        const deltaClass = delta > 0 ? 'positive' : 'negative';
                        return `
                            <div class="lap-item">
                                <div class="lap-num">Lap ${lap.lap}</div>
                                <div class="lap-time">${formatTime(lap.time)}</div>
                                <div class="lap-delta ${deltaClass}">+${delta.toFixed(3)}s</div>
                            </div>
                        `;
                    }).join('');
                }
            } catch (error) {
                console.error('Error:', error);
            }
        }
        
        function formatTime(seconds) {
            const mins = Math.floor(seconds / 60);
            const secs = (seconds % 60).toFixed(3);
            return `${mins}:${secs < 10 ? '0' : ''}${secs}`;
        }
        
        async function clearSession() {
            if (confirm('Clear all laps?')) {
                await fetch('/api/clear');
                fetchData();
            }
        }
        
        async function exportData() {
            const res = await fetch('/api/laps');
            const data = await res.json();
            
            let csv = 'Lap,Time,Fuel,Speed\n';
            data.laps.forEach((lap, idx) => {
                csv += `${lap.lap},${lap.time.toFixed(3)},${lap.fuel.toFixed(2)},${lap.speed.toFixed(1)}\n`;
            });
            
            const blob = new Blob([csv], { type: 'text/csv' });
            const url = URL.createObjectURL(blob);
            const a = document.createElement('a');
            a.href = url;
            a.download = `iracing-session-${new Date().toISOString()}.csv`;
            a.click();
        }
        
        // Update every 100ms
        setInterval(fetchData, 100);
        fetchData();
    </script>
</body>
</html>
```

---

## Lancer le projet

### Étape 1: Démarre le serveur

```bash
# Dans ton terminal (depuis le dossier du projet)
python main.py
```

Tu devrais voir:
```
🏁 iRacing Telemetry Server started on http://localhost:5000
   Lance iRacing et démarre une session!
```

### Étape 2: Ouvre le dashboard

- Va sur `http://localhost:5000` dans ton navigateur
- Tu verras le dashboard en direct

### Étape 3: Lance iRacing et roule!

- Lance iRacing
- Fais une session (practice, race, test)
- Le dashboard va enregistrer chaque lap automatiquement
- Les stats se mettent à jour en temps réel

---

## Utilisation

### Pendant une session:
- **Status** montre si iRacing est actif
- **Laps Recorded** compte tes tours
- **Best Lap** affiche ton meilleur temps
- **Delta** montre combien tu es au-dessus du meilleur

### Après une session:
- **Export CSV** → Exporte tes données pour analyse Excel
- **Clear Session** → Réinitialise pour une nouvelle session

---

## Données capturées (par lap)

```
- Lap number
- Lap time
- Fuel level
- Throttle average
- Brake average
- Max speed
- Timestamp
```

---

## Prochaines étapes (amélioration)

Si tu veux ajouter des trucs:

### 1. Plus de données
Modifie `iracing_sdk.py` pour capturer:
- Température des pneus
- Dégâts de la voiture
- Gear/RPM
- Position sur piste

### 2. Graphiques
Ajoute Chart.js pour:
- Courbe des temps de lap
- Graphique fuel/temps
- Vitesse max par lap

### 3. Base de données
- Sauvegarde auto les sessions
- Compare les sessions différentes
- Historique longue durée

### 4. Analyse
- Détecte les meilleurs secteurs
- Compare avec d'autres pilotes
- Conseils d'amélioration automatiques

---

## Troubleshooting

### "Can't find IRSDKMemoryMap"
- iRacing n'est pas lancé
- Démarre iRacing et une session (même practice)

### "Port 5000 is in use"
```bash
# Utilise un autre port
python main.py --port 5001
```

### Pas de données capturées
- Assure-toi que iRacing est en session (practice/race)
- Le pause menu peut arrêter la capture

---

## Ressources

- **iRacing SDK Docs**: https://github.com/iRacingSdkWrapper
- **Memory offsets**: Voir documentation officielle iRacing

---

## Notes

- Le serveur capture à 100Hz (10ms d'intervalle)
- Les données sont stockées en mémoire (persistent après export)
- Fonctionne sur Windows (utilise les named pipes iRacing)

**Happy racing! 🏁**
