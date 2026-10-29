'use strict';
let quickKind,quickGeneration=0;
document.addEventListener('click',e=>{
  const button=e.target.closest('[data-quick]');
  if(!button||editorKind!=='lesson'||!$('#editor').open)return;
  quickKind=button.dataset.quick;
  quickGeneration++;
  $('#quick-error').textContent='';
  $('#quick-title').textContent=quickKind==='person'?'Создать человека':'Создать предмет';
  $('#quick-fields').innerHTML=quickKind==='person'
    ?field('name','Имя для входа','','text','required maxlength="100"')
    :field('title','Название предмета','','text','required maxlength="150"')
     +field('url','Постоянная ссылка','','text','required placeholder="https://bbb.example/b/room"')
     +'<label class="field">Тип комнаты<select name="adapter"><option value="bbb">BigBlueButton</option><option value="demo">Локальная демонстрация (demo://имя)</option></select></label>';
  $('#quick-editor').showModal();
});
$('#quick-form').addEventListener('submit',async e=>{
  e.preventDefault();
  const form=e.target,submit=form.querySelector('[type=submit]');
  if(submit.disabled)return;
  submit.disabled=true;
  const kind=quickKind,generation=quickGeneration;
  const peopleBox=$('#lesson-people'),subjectSelect=$('#editor-form').elements.subject_id;
  try{
    const next=structuredClone(config),id=kind[0]+'_'+crypto.randomUUID().slice(0,8);
    if(kind==='person')next.people.push({id,name:form.elements.name.value,enabled:true});
    else next.subjects.push({id,title:form.elements.title.value,url:form.elements.url.value,adapter:form.elements.adapter.value});
    await save(next);
    // The parent dialog stays mounted: times, recurrence, selections and even
    // incomplete inputs survive both validation errors and cancelling the child.
    if(kind==='person'&&peopleBox?.isConnected){
      const person=config.people.find(p=>p.id===id);
      peopleBox.insertAdjacentHTML('beforeend',`<label><input type="checkbox" name="person" value="${esc(id)}" checked>${esc(person.name)}</label>`);
    }else if(kind==='subject'&&subjectSelect?.isConnected){
      const subject=config.subjects.find(s=>s.id===id),select=subjectSelect;
      select.querySelector('option[value=""]')?.remove();
      select.add(new Option(subject.title,id));select.value=id;
    }
    if(generation===quickGeneration)$('#quick-editor').close();
  }catch(error){if(generation===quickGeneration)$('#quick-error').textContent=error.message;}
  finally{submit.disabled=false;}
});
