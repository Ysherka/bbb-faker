'use strict';
let incomingBundle=null, importRevision=null, previewGeneration=0;
$('#transfer-open').onclick=()=>$('#transfer-dialog').showModal();
$('#data-export').onclick=async()=>{
  try{
    const sections=[...document.querySelectorAll('[name=export-section]:checked')].map(el=>el.value);
    if(!sections.length)throw Error('Выберите хотя бы один раздел для экспорта.');
    const bundle=await api('export/data?sections='+sections.join(','));
    download('bbb-schedule.json',JSON.stringify(bundle,null,2));
  }catch(error){toast(error.message,true);}
};
function resetImport(){previewGeneration++;importRevision=null;$('#data-apply').disabled=true;$('#import-preview').textContent='';$('#import-error').textContent='';}
async function previewImport(){
  resetImport();if(!incomingBundle)return;
  const generation=previewGeneration,mode=$('#import-mode').value;
  try{
    const preview=await api('import/preview','POST',{bundle:incomingBundle,mode});
    if(generation!==previewGeneration)return;
    importRevision=preview.revision;
    $('#import-preview').innerHTML=`${mode==='replace'?'<p class="replace-note">Все существующие люди, предметы и занятия будут заменены содержимым файла.</p>':''}<table class="import-table"><thead><tr><th>Раздел</th><th>В файле</th><th>Добавится</th><th>Совпало</th><th>Новых ID</th><th>Всего после</th></tr></thead><tbody>${Object.entries(preview.counts).map(([key,c])=>`<tr><th>${{people:'Люди',subjects:'Предметы',lessons:'Занятия'}[key]}</th><td>${c.incoming}</td><td>${c.added}</td><td>${c.reused}</td><td>${c.renamed}</td><td>${c.total}</td></tr>`).join('')}</tbody></table>`;
    $('#data-apply').disabled=false;
  }catch(error){if(generation===previewGeneration)$('#import-error').textContent=error.message;}
}
$('#data-import').onchange=async e=>{
  resetImport();incomingBundle=null;$('#import-refresh').disabled=true;
  const file=e.target.files[0];if(!file)return;
  try{
    if(file.size>5*1024*1024)throw Error('Файл слишком большой (максимум 5 МБ).');
    incomingBundle=JSON.parse((await file.text()).replace(/^\uFEFF/,''));
    if(incomingBundle.format!=='bbb-companion-transfer')throw Error('Выберите файл, полученный через «Импорт и экспорт». Полную конфигурацию можно загрузить в редакторе JSON.');
    $('#import-refresh').disabled=false;
    await previewImport();
  }catch(error){$('#import-error').textContent=error.message;}
};
$('#import-mode').onchange=previewImport;$('#import-refresh').onclick=previewImport;
$('#data-apply').onclick=async()=>{
  if(!incomingBundle||!importRevision)return;
  $('#data-apply').disabled=true;
  try{
    config=await api('import/data','POST',{bundle:incomingBundle,mode:$('#import-mode').value,revision:importRevision});
    renderConfig();resetImport();incomingBundle=null;$('#data-import').value='';$('#import-refresh').disabled=true;
    $('#transfer-dialog').close();toast('Данные импортированы. Текущие подключения продолжают работу.');
  }catch(error){importRevision=null;$('#import-error').textContent=error.message;}
};

async function renderDesktop(){
  try{
    const value=await api('desktop');
    $('#autostart').checked=value.autostart;$('#autostart').disabled=!value.supported;
    $('#autostart-status').textContent=!value.supported?'Доступно только в Windows.':!value.autostart_current_path?'Автозапуск указывает на прежний путь. Выключите и включите его снова для обновления.':value.autostart?'Будет запущена эта копия приложения с текущей папкой данных.':'Автозапуск выключен.';
  }catch(error){$('#autostart-status').textContent=error.message;}
}
$('#autostart').onchange=async e=>{
  e.target.disabled=true;
  try{await api('desktop/autostart','PUT',{enabled:e.target.checked});toast(e.target.checked?'Автозапуск включён':'Автозапуск выключен');}catch(error){toast(error.message,true);}
  await renderDesktop();
};
