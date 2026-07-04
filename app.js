const form = document.getElementById('clipForm');
const videoUrl = document.getElementById('videoUrl');
const videoFile = document.getElementById('videoFile');
const fileName = document.getElementById('fileName');
const command = document.getElementById('command');
const durationSeconds = document.getElementById('durationSeconds');
const clipCount = document.getElementById('clipCount');
const customClipCount = document.getElementById('customClipCount');
const aspectRatio = document.getElementById('aspectRatio');
const captions = document.getElementById('captions');
const subtitleModel = document.getElementById('subtitleModel');
const language = document.getElementById('language');
const speedMode = document.getElementById('speedMode');
const encoderMode = document.getElementById('encoderMode');
const downloadQuality = document.getElementById('downloadQuality');
const submitBtn = document.getElementById('submitBtn');
const progressCard = document.getElementById('progressCard');
const progressTitle = document.getElementById('progressTitle');
const progressText = document.getElementById('progressText');
const errorCard = document.getElementById('errorCard');
const errorText = document.getElementById('errorText');
const results = document.getElementById('results');
const clipsGrid = document.getElementById('clipsGrid');
const zipLink = document.getElementById('zipLink');
const healthStatus = document.getElementById('healthStatus');
const metricAspect = document.getElementById('metricAspect');
const metricDuration = document.getElementById('metricDuration');
const metricCaption = document.getElementById('metricCaption');
const metricSpeed = document.getElementById('metricSpeed');
const summaryAspect = document.getElementById('summaryAspect');
const summaryCaption = document.getElementById('summaryCaption');
const summaryCount = document.getElementById('summaryCount');
const summarySpeed = document.getElementById('summarySpeed');
const resultMeta = document.getElementById('resultMeta');

function secondsToLabel(value) {
  const total = parseInt(value || '60', 10);
  const m = Math.floor(total / 60);
  const s = total % 60;
  return `${String(m).padStart(2, '0')}:${String(s).padStart(2, '0')}`;
}

function getClipCount() {
  const manualRaw = (customClipCount.value || '').trim();
  if (manualRaw) {
    const manual = Math.max(1, Math.min(100, parseInt(manualRaw, 10) || 1));
    customClipCount.value = String(manual);
    return manual;
  }
  return parseInt(clipCount.value || '10', 10);
}

function getClipCountLabel() {
  const count = getClipCount();
  return count === 0 ? 'todos os cortes possíveis até 100' : `${count} cortes`;
}

function setClipCountFromCommand(value) {
  const count = Math.max(1, Math.min(100, parseInt(value, 10) || 10));
  const presetOption = Array.from(clipCount.options).find(option => option.value === String(count));
  if (presetOption) {
    clipCount.value = String(count);
    customClipCount.value = '';
  } else {
    customClipCount.value = String(count);
  }
}

function speedLabel(value) {
  if (value === 'quality') return 'Qualidade';
  if (value === 'balanced') return 'Equilibrado';
  if (value === 'turbo') return 'Turbo';
  return 'Ultra rápido';
}

function updateSummary() {
  metricAspect.textContent = aspectRatio.value;
  summaryAspect.textContent = aspectRatio.value;
  metricDuration.textContent = secondsToLabel(durationSeconds.value);
  const captionText = captions.checked ? 'Ativada' : 'Desativada';
  metricCaption.textContent = captionText;
  summaryCaption.textContent = captions.checked ? 'Falas ativadas' : 'Sem legenda';
  metricSpeed.textContent = speedLabel(speedMode.value);
  summarySpeed.textContent = speedLabel(speedMode.value);
  const count = getClipCount();
  summaryCount.textContent = count === 0 ? 'Todos até 100' : `${count} vídeos`;
}

videoFile.addEventListener('change', () => {
  const file = videoFile.files && videoFile.files[0];
  fileName.textContent = file ? file.name : 'MP4, MOV, MKV ou WEBM';
});

command.addEventListener('input', () => {
  const text = command.value.toLowerCase();
  const secondsMatch = text.match(/(\d+)\s*(segundo|segundos|s)\b/);
  const minuteMatch = text.match(/(\d+)\s*(minuto|minutos|min)\b/);
  if (secondsMatch) durationSeconds.value = String(parseInt(secondsMatch[1], 10));
  if (minuteMatch) durationSeconds.value = String(parseInt(minuteMatch[1], 10) * 60);
  const countMatch = text.match(/(\d+)\s*(cortes|videos|vídeos|clips)/);
  if (countMatch) setClipCountFromCommand(countMatch[1]);

  if (/(tiktok|reels|shorts|celular|telefone|vertical|9:16)/.test(text)) aspectRatio.value = '9:16';
  if (/(pc|youtube|horizontal|paisagem|16:9)/.test(text)) aspectRatio.value = '16:9';
  if (/(quadrado|feed|1:1)/.test(text)) aspectRatio.value = '1:1';
  if (/(original|manter proporção|manter proporcao)/.test(text)) aspectRatio.value = 'original';

  if (/(legendado|legenda|legendar|falas|transcrever)/.test(text)) captions.checked = true;
  if (/(sem legenda|nao legendar|não legendar)/.test(text)) captions.checked = false;
  if (/(rápido|rapido|turbo|velocidade|menos de 2 minutos|ultra)/.test(text)) speedMode.value = 'ultra';

  updateSummary();
});

[durationSeconds, clipCount, customClipCount, aspectRatio, captions, speedMode, encoderMode, downloadQuality].forEach((element) => {
  element.addEventListener('input', updateSummary);
  element.addEventListener('change', updateSummary);
});

async function checkHealth() {
  try {
    const res = await fetch('/api/health?ts=' + Date.now());
    const data = await res.json();
    if (data.ok) {
      healthStatus.textContent = `Motor ativo: ${data.encoder || 'FFmpeg'}`;
      healthStatus.classList.add('ok');
    } else {
      healthStatus.textContent = 'Motor com erro';
      healthStatus.classList.add('bad');
    }
  } catch (err) {
    healthStatus.textContent = 'Servidor offline';
    healthStatus.classList.add('bad');
  }
}

function showError(message) {
  errorText.textContent = message || 'Erro desconhecido.';
  errorCard.classList.remove('hidden');
}

function resetUi() {
  errorCard.classList.add('hidden');
  results.classList.add('hidden');
  clipsGrid.innerHTML = '';
}

function renderResults(data) {
  zipLink.href = data.zip_url;
  resultMeta.textContent = `${data.clip_count} cortes • ${data.aspect_name || data.aspect_ratio} • ${data.speed_label || 'Ultra'} • ${data.encoder || 'encoder auto'} • ${data.workers || 1} render simultâneo(s) • legenda ${data.captions_enabled ? 'ativada' : 'desativada'}`;
  clipsGrid.innerHTML = '';
  data.clips.forEach((clip) => {
    const card = document.createElement('article');
    card.className = 'clip-card';
    card.innerHTML = `
      <div class="thumb">
        <span class="play">▶</span>
        <span class="duration-badge">${clip.duration}</span>
      </div>
      <div class="clip-body">
        <h3>${clip.title}</h3>
        <div class="info-line"><span>Duração</span><strong>${clip.duration}</strong></div>
        <div class="info-line"><span>Trecho original</span><strong>${clip.source_range}</strong></div>
        <div class="info-line"><span>Proporção</span><strong>${clip.aspect_ratio}</strong></div>
        <div class="info-line"><span>Legenda das falas</span><strong>${clip.captions ? 'Sim' : 'Não'}</strong></div>
        <div class="info-line"><span>Render</span><strong>${clip.encoder || data.encoder || 'Auto'}</strong></div>
        <div class="info-line"><span>Tamanho</span><strong>${clip.size_mb} MB</strong></div>
        <div class="clip-actions">
          <a href="${clip.download_url}" download>Baixar MP4</a>
          <a href="${clip.download_url}" target="_blank">Visualizar</a>
        </div>
      </div>
    `;
    clipsGrid.appendChild(card);
  });
  results.classList.remove('hidden');
}

form.addEventListener('submit', async (event) => {
  event.preventDefault();
  resetUi();

  const url = (videoUrl.value || '').trim();
  const file = videoFile.files && videoFile.files[0];

  if (!url && !file) {
    showError('Cole um link do YouTube ou envie um arquivo de vídeo.');
    return;
  }

  const fd = new FormData();
  fd.append('video_url', url);
  fd.append('duration_seconds', durationSeconds.value || '60');
  fd.append('clip_count', String(getClipCount()));
  fd.append('aspect_ratio', aspectRatio.value || '9:16');
  fd.append('captions', captions.checked ? 'true' : 'false');
  fd.append('subtitle_model', subtitleModel.value || 'tiny');
  fd.append('language', language.value || 'pt');
  fd.append('speed_mode', speedMode.value || 'ultra');
  fd.append('encoder_mode', encoderMode.value || 'auto');
  fd.append('download_quality', downloadQuality.value || 'fast');
  if (file) fd.append('video_file', file);

  submitBtn.disabled = true;
  submitBtn.textContent = 'Processando...';
  progressCard.classList.remove('hidden');
  progressTitle.textContent = url && !file ? 'Baixando vídeo do YouTube...' : 'Processando arquivo enviado...';
  progressText.textContent = captions.checked
    ? `Modo ${speedLabel(speedMode.value)}. O ClipNex baixa o vídeo, transcreve uma única vez somente o trecho necessário, cria as legendas, usa GPU se disponível e renderiza ${getClipCountLabel()} em paralelo. A primeira execução pode baixar o modelo tiny.`
    : `Modo ${speedLabel(speedMode.value)}. O ClipNex baixa o vídeo, usa GPU se disponível e renderiza ${getClipCountLabel()} em paralelo. Sem legenda fica muito mais rápido.`;

  try {
    const startRes = await fetch('/api/jobs?ts=' + Date.now(), { method: 'POST', body: fd });
    const startData = await startRes.json().catch(() => ({}));
    if (!startRes.ok) {
      throw new Error(startData.error || startData.detail || 'Falha ao iniciar processamento.');
    }

    const jobId = startData.job_id;
    progressTitle.textContent = 'Processamento iniciado';
    progressText.textContent = startData.message || 'O ClipNex colocou seu vídeo na fila e vai atualizar esta tela automaticamente.';

    let finished = false;
    while (!finished) {
      await new Promise(resolve => setTimeout(resolve, 1600));
      const pollRes = await fetch(`/api/jobs/${jobId}?ts=${Date.now()}`);
      const job = await pollRes.json().catch(() => ({}));
      if (!pollRes.ok) {
        throw new Error(job.error || job.detail || 'Falha ao consultar status.');
      }
      progressTitle.textContent = job.status === 'queued' ? 'Na fila...' : job.status === 'running' ? 'Processando...' : 'Finalizando...';
      progressText.textContent = job.message || 'Processando o vídeo. Não feche esta página.';
      if (job.status === 'done') {
        finished = true;
        progressCard.classList.add('hidden');
        renderResults(job.result);
      } else if (job.status === 'error') {
        throw new Error(job.error || 'Falha ao processar.');
      }
    }
  } catch (err) {
    progressCard.classList.add('hidden');
    showError(err.message);
  } finally {
    submitBtn.disabled = false;
    submitBtn.textContent = 'Gerar cortes reais agora';
  }
});

updateSummary();
checkHealth();
