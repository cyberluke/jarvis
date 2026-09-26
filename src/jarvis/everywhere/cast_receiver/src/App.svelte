<script>
  import { onMount, onDestroy } from 'svelte';
  
  // Cast framework
  let castContext = null;
  let playerManager = null;
  let subtitleChannel = null;
  
  // Video state
  let videoUrl = '';
  let videoType = 'video/mp4';
  let isPlaying = false;
  let currentTime = 0;
  let duration = 0;
  let volume = 1.0;
  
  // Subtitle state
  let currentSubtitle = '';
  let subtitleDelay = 0; // milliseconds to delay subtitles
  let subtitleQueue = [];
  let showSubtitles = true;
  
  // UI state
  let showControls = true;
  let controlsTimeout = null;
  
  onMount(() => {
    initCastReceiver();
    startControlsAutoHide();
  });
  
  onDestroy(() => {
    if (castContext) {
      castContext.stop();
    }
    if (controlsTimeout) {
      clearTimeout(controlsTimeout);
    }
  });
  
  function initCastReceiver() {
    castContext = cast.framework.CastReceiverContext.getInstance();
    playerManager = castContext.getPlayerManager();
    
    // Set up subtitle channel
    const SUBTITLE_NAMESPACE = 'urn:x-cast:com.toastovac.subtitles';
    castContext.addCustomMessageListener(SUBTITLE_NAMESPACE, (event) => {
      handleSubtitleMessage(event.data);
    });
    
    // Set up media event listeners
    playerManager.addEventListener(
      cast.framework.events.EventType.PLAYING,
      () => { isPlaying = true; }
    );
    playerManager.addEventListener(
      cast.framework.events.EventType.PAUSE,
      () => { isPlaying = false; }
    );
    playerManager.addEventListener(
      cast.framework.events.EventType.TIME_UPDATE,
      (event) => { 
        currentTime = event.currentMediaTime || 0;
        processSubtitleQueue();
      }
    );
    playerManager.addEventListener(
      cast.framework.events.EventType.DURATION_CHANGE,
      (event) => { duration = event.duration || 0; }
    );
    
    // Start the receiver
    castContext.start();
    
    // Expose global functions for external control
    window.loadVideo = loadVideo;
    window.setSubtitleDelay = setSubtitleDelay;
    window.toggleSubtitles = toggleSubtitles;
  }
  
  function loadVideo(url, type = 'video/mp4', startTime = 0) {
    videoUrl = url;
    videoType = type;
    
    const mediaInfo = new cast.framework.messages.MediaInformation();
    mediaInfo.contentId = url;
    mediaInfo.contentType = type;
    mediaInfo.streamType = cast.framework.messages.StreamType.BUFFERED;
    
    const request = new cast.framework.messages.LoadRequestData();
    request.media = mediaInfo;
    request.currentTime = startTime;
    request.autoplay = true;
    
    playerManager.load(request)
      .then(() => {
        console.log('Video loaded:', url);
        isPlaying = true;
      })
      .catch((error) => {
        console.error('Failed to load video:', error);
      });
  }
  
  function handleSubtitleMessage(data) {
    if (data.action === 'set_delay') {
      subtitleDelay = data.delay_ms || 0;
      return;
    }
    
    if (data.action === 'clear') {
      currentSubtitle = '';
      subtitleQueue = [];
      return;
    }
    
    if (data.action === 'toggle') {
      showSubtitles = !showSubtitles;
      return;
    }
    
    // Add subtitle to queue with timestamp
    const subtitle = {
      text: data.text || '',
      sourceText: data.source_text || '',
      timestamp: data.timestamp || 0,
      duration: data.duration || 5000, // display duration in ms
      language: data.language || 'unknown',
      receivedAt: Date.now()
    };
    
    subtitleQueue.push(subtitle);
    processSubtitleQueue();
  }
  
  function processSubtitleQueue() {
    if (!showSubtitles) {
      currentSubtitle = '';
      return;
    }
    
    const now = Date.now();
    const adjustedTime = currentTime + (subtitleDelay / 1000);
    
    // Find the subtitle that should be displayed now
    let activeSubtitle = null;
    let remainingQueue = [];
    
    for (const sub of subtitleQueue) {
      const subTime = sub.timestamp;
      const subEnd = subTime + (sub.duration / 1000);
      
      if (adjustedTime >= subTime && adjustedTime <= subEnd) {
        activeSubtitle = sub;
      } else if (adjustedTime < subTime) {
        remainingQueue.push(sub);
      }
      // Old subtitles are dropped
    }
    
    subtitleQueue = remainingQueue;
    
    if (activeSubtitle) {
      currentSubtitle = activeSubtitle.text;
    } else if (currentSubtitle && !activeSubtitle) {
      // Keep showing last subtitle briefly
      const lastSub = subtitleQueue[subtitleQueue.length - 1];
      if (lastSub && (now - lastSub.receivedAt) < 2000) {
        // Keep current subtitle
      } else {
        currentSubtitle = '';
      }
    }
  }
  
  function setSubtitleDelay(delayMs) {
    subtitleDelay = delayMs;
  }
  
  function toggleSubtitles() {
    showSubtitles = !showSubtitles;
    if (!showSubtitles) {
      currentSubtitle = '';
    }
  }
  
  function startControlsAutoHide() {
    const hideControls = () => {
      showControls = false;
    };
    
    const resetTimer = () => {
      showControls = true;
      if (controlsTimeout) clearTimeout(controlsTimeout);
      controlsTimeout = setTimeout(hideControls, 3000);
    };
    
    // Reset timer on user interaction
    document.addEventListener('mousemove', resetTimer);
    document.addEventListener('touchstart', resetTimer);
    document.addEventListener('keydown', resetTimer);
    
    resetTimer();
  }
  
  function formatTime(seconds) {
    const hrs = Math.floor(seconds / 3600);
    const mins = Math.floor((seconds % 3600) / 60);
    const secs = Math.floor(seconds % 60);
    
    if (hrs > 0) {
      return `${hrs}:${mins.toString().padStart(2, '0')}:${secs.toString().padStart(2, '0')}`;
    }
    return `${mins}:${secs.toString().padStart(2, '0')}`;
  }
  
  function handleKeydown(event) {
    switch(event.key) {
      case ' ':
      case 'Enter':
        // Toggle play/pause
        if (isPlaying) {
          playerManager.pause();
        } else {
          playerManager.play();
        }
        break;
      case 'ArrowLeft':
        playerManager.seek(currentTime - 10);
        break;
      case 'ArrowRight':
        playerManager.seek(currentTime + 10);
        break;
      case 'ArrowUp':
        playerManager.setVolume(Math.min(1, volume + 0.1));
        break;
      case 'ArrowDown':
        playerManager.setVolume(Math.max(0, volume - 0.1));
        break;
      case 's':
        toggleSubtitles();
        break;
    }
  }
</script>

<svelte:window on:keydown={handleKeydown}/>

<div class="receiver" class:show-controls={showControls}>
  <!-- Video Player (Cast CAF managed) -->
  {#if videoUrl}
    <cast-media-player></cast-media-player>
  {:else}
    <div class="idle-screen">
      <div class="logo">Toastovac</div>
      <div class="status">Waiting for media...</div>
      <div class="hint">Cast from your PC to start</div>
    </div>
  {/if}
  
  <!-- Subtitle Overlay -->
  {#if showSubtitles && currentSubtitle}
    <div class="subtitle-overlay">
      <div class="subtitle-text">{currentSubtitle}</div>
    </div>
  {/if}
  
  <!-- Controls Overlay -->
  {#if showControls && videoUrl}
    <div class="controls-overlay">
      <div class="progress-bar">
        <div class="progress" style="width: {(currentTime / duration) * 100}%"></div>
      </div>
      <div class="controls-row">
        <button class="control-btn" on:click={() => playerManager.pause()}>
          {isPlaying ? '⏸' : '▶'}
        </button>
        <span class="time">{formatTime(currentTime)} / {formatTime(duration)}</span>
        <button class="control-btn" on:click={toggleSubtitles}>
          {showSubtitles ? 'CC On' : 'CC Off'}
        </button>
      </div>
    </div>
  {/if}
</div>

<style>
  .receiver {
    width: 100%;
    height: 100%;
    position: relative;
    background: #000;
  }
  
  video {
    width: 100%;
    height: 100%;
    object-fit: contain;
  }
  
  .idle-screen {
    width: 100%;
    height: 100%;
    display: flex;
    flex-direction: column;
    align-items: center;
    justify-content: center;
    color: #fff;
    font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, sans-serif;
  }
  
  .logo {
    font-size: 4rem;
    font-weight: bold;
    margin-bottom: 1rem;
    background: linear-gradient(135deg, #667eea, #764ba2);
    -webkit-background-clip: text;
    -webkit-text-fill-color: transparent;
  }
  
  .status {
    font-size: 1.5rem;
    color: #aaa;
    margin-bottom: 0.5rem;
  }
  
  .hint {
    font-size: 1rem;
    color: #666;
  }
  
  .subtitle-overlay {
    position: absolute;
    bottom: 10%;
    left: 5%;
    right: 5%;
    display: flex;
    justify-content: center;
    pointer-events: none;
    z-index: 100;
  }
  
  .subtitle-text {
    font-size: 3.5vw;
    font-weight: 500;
    color: #fff;
    text-align: center;
    line-height: 1.4;
    text-shadow: 
      0 2px 4px rgba(0,0,0,0.9),
      0 4px 8px rgba(0,0,0,0.7),
      0 0 20px rgba(0,0,0,0.5);
    padding: 0.5em 1em;
    background: rgba(0,0,0,0.6);
    border-radius: 8px;
    max-width: 90%;
    word-wrap: break-word;
  }
  
  .controls-overlay {
    position: absolute;
    bottom: 0;
    left: 0;
    right: 0;
    background: linear-gradient(transparent, rgba(0,0,0,0.8));
    padding: 2rem 2rem 1rem;
    opacity: 0;
    transition: opacity 0.3s;
  }
  
  .show-controls .controls-overlay {
    opacity: 1;
  }
  
  .progress-bar {
    width: 100%;
    height: 4px;
    background: rgba(255,255,255,0.2);
    border-radius: 2px;
    margin-bottom: 1rem;
    cursor: pointer;
  }
  
  .progress {
    height: 100%;
    background: #667eea;
    border-radius: 2px;
    transition: width 0.1s linear;
  }
  
  .controls-row {
    display: flex;
    align-items: center;
    gap: 1rem;
  }
  
  .control-btn {
    background: rgba(255,255,255,0.1);
    border: none;
    color: #fff;
    font-size: 1.5rem;
    padding: 0.5rem 1rem;
    border-radius: 4px;
    cursor: pointer;
    transition: background 0.2s;
  }
  
  .control-btn:hover {
    background: rgba(255,255,255,0.2);
  }
  
  .time {
    color: #fff;
    font-family: monospace;
    font-size: 1rem;
  }
</style>
