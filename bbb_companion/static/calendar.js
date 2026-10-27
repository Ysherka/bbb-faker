'use strict';
let weekStart = samaraToday(), weekGeneration = 0;

function samaraToday(){return new Intl.DateTimeFormat('en-CA',{timeZone:'Europe/Samara',year:'numeric',month:'2-digit',day:'2-digit'}).format(new Date());}
function shiftDate(day,count){const d=new Date(day+'T12:00:00Z');d.setUTCDate(d.getUTCDate()+count);return d.toISOString().slice(0,10);}
function weekday(day){return (new Date(day+'T12:00:00Z').getUTCDay()+6)%7;}
function dateLabel(day){return new Date(day+'T12:00:00Z').toLocaleDateString('ru-RU',{day:'numeric',month:'long',timeZone:'UTC'});}

function setScheduleView(view){
  const week=view==='week';
  $('#week-view').hidden=!week;$('#lessons').hidden=week;
  $('#view-week').setAttribute('aria-pressed',String(week));$('#view-list').setAttribute('aria-pressed',String(!week));
  localStorage.setItem('schedule-view',view);
}
$('#view-week').onclick=()=>setScheduleView('week');
$('#view-list').onclick=()=>setScheduleView('list');
setScheduleView(localStorage.getItem('schedule-view')||'week');
$('#week-prev').onclick=()=>{weekStart=shiftDate(weekStart,-7);renderCalendar();};
$('#week-next').onclick=()=>{weekStart=shiftDate(weekStart,7);renderCalendar();};
$('#week-today').onclick=()=>{weekStart=samaraToday();renderCalendar();};
$('#schedule-person').onchange=()=>{renderLessonList();renderCalendar();};

async function renderCalendar(){
  const generation=++weekGeneration;
  const filter=$('#schedule-person'), selected=filter.value;
  filter.innerHTML='<option value="">Все люди</option>'+config.people.map(p=>`<option value="${esc(p.id)}">${esc(p.name)}</option>`).join('');
  filter.value=config.people.some(p=>p.id===selected)?selected:'';
  try{
    const data=await api('week?start='+encodeURIComponent(weekStart));
    if(generation!==weekGeneration)return;
    weekStart=data.start;
    $('#week-title').textContent=`${dateLabel(data.days[0])} — ${dateLabel(data.days[6])}`;
    const items=data.lessons.filter(o=>!filter.value||o.person_ids.includes(filter.value));
    const cells=new Map(), times=new Set(['08:00','10:00','12:00','14:00','16:00','18:00']);
    for(const item of items){
      for(const day of data.days){
        const startDay=item.start.slice(0,10),endDay=item.end.slice(0,10),endTime=item.end.slice(11,16);
        if(day<startDay||day>endDay||(day===endDay&&day!==startDay&&endTime==='00:00'))continue;
        const start=day===startDay?item.start.slice(11,16):'00:00';
        times.add(start);
        const key=day+'/'+start;
        if(!cells.has(key))cells.set(key,[]);
        cells.get(key).push({...item,segmentDay:day,segmentStart:start,continued:day!==startDay});
      }
    }
    const today=samaraToday();
    $('#week-grid').innerHTML=`<table class="week-table"><caption class="sr-only">Расписание на неделю, время Самары</caption><thead><tr><th scope="col">Время</th>${data.days.map((day,i)=>`<th scope="col" class="${day===today?'today':''}"><span>${days[i]}</span><small>${dateLabel(day)}</small><button class="quiet" data-add-day="${day}" aria-label="Добавить занятие ${day}">+ Добавить</button></th>`).join('')}</tr></thead><tbody>${[...times].sort().map(time=>`<tr><th scope="row">${time}</th>${data.days.map(day=>`<td class="${day===today?'today':''}">${(cells.get(day+'/'+time)||[]).map(item=>`<button class="week-lesson" data-edit="lesson" data-id="${esc(item.lesson_id)}"><span class="week-time">${item.continued?'Продолжение · ':''}${item.segmentStart}–${item.end.slice(0,10)>day?'24:00':item.end.slice(11,16)}</span><strong>${esc(subjectOf(item.subject_id))}</strong><small>${item.person_ids.map(id=>esc(nameOf(id))).join(', ')}</small></button>`).join('')}<button class="cell-add" data-add-day="${day}" data-time="${time}" aria-label="Добавить занятие ${day} в ${time}">+</button></td>`).join('')}</tr>`).join('')}</tbody></table>`;
    $('#week-error').textContent='';
  }catch(error){if(generation===weekGeneration)$('#week-error').textContent=error.message;}
}

document.addEventListener('click',e=>{
  const button=e.target.closest('[data-add-day]');if(!button)return;
  const date=button.dataset.addDay,start=button.dataset.time||'10:00';
  const endMinutes=(Number(start.slice(0,2))*60+Number(start.slice(3))+90)%(24*60);
  const end=`${String(Math.floor(endMinutes/60)).padStart(2,'0')}:${String(endMinutes%60).padStart(2,'0')}`;
  openEditor('lesson',undefined,{date,start,end,weekdays:[weekday(date)],person_ids:$('#schedule-person').value?[$('#schedule-person').value]:[]});
});
