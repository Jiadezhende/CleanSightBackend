# CleanSightBackend

CleanSight is a backend system for AI-powered inspection of the endoscope cleaning process at Changhai Hospital. It ensures each cleaning step is performed correctly, enhancing patient safety and compliance.

## Features

- **Real-time Video Stream Processing**: Capture video from camera or local file, process with AI models, and stream results via WebSocket
- **Three-Thread Architecture**: Decoupled frame capture, AI inference, and WebSocket streaming for optimal performance
- **RESTful APIs**: Standard HTTP endpoints for health checks and future extensions
- **Modular Design**: Easy to extend with new AI models and processing pipelines

## Installation

```powershell
py -3.11 -m venv .\venv
.\venv\Scripts\activate
pip install -r requirements.txt
```

## Running the Application

In the activated virtual environment, run:

```powershell
python run.py
```

The API will be available at <http://localhost:8000>

## API Documentation

Once running, visit <http://localhost:8000/docs> for interactive API documentation.

## Real-time Video Streaming

### WebSocket Endpoint

- **URL**: `ws://localhost:8000/ws/video`
- **Description**: Real-time video stream with AI processing results
- **Data Format**: Base64 encoded JPEG images (`data:image/jpeg;base64,...`)

### Usage

1. Start the server with `python run.py`
2. Connect to the WebSocket endpoint from your frontend application
3. The server will automatically start capturing video (camera or demo.mp4) and streaming processed frames

### Architecture

- **Capture Thread**: Continuously captures latest frames from video source
- **Inference Thread**: Processes frames with AI models (currently mock implementation)
- **WebSocket Thread**: Streams processed results to connected clients
- **Frame Skipping**: Automatically discards old frames to maintain real-time performance

### Video Source

- Primary: Camera (device 0)
- Fallback: Local video file `demo.mp4` (place in project root)
