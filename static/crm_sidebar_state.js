(function(root){
'use strict';

const STORAGE_KEY='crm-sidebar-collapsed';
const collapsedClass='sidebar-collapsed';
const labels={
 collapsed:'Déplier la barre latérale',
 expanded:'Replier la barre latérale',
};

const resolveStorage=storage=>{
 if(storage!==undefined)return storage;
 try{return root.localStorage||null}catch(error){return null}
};
const readPreference=storage=>{
 try{return resolveStorage(storage)?.getItem(STORAGE_KEY)==='1'}catch(error){return false}
};
const writePreference=(storage,collapsed)=>{
 try{resolveStorage(storage)?.setItem(STORAGE_KEY,collapsed?'1':'0')}catch(error){}
};
const apply=(document,collapsed,{storage,persist=false}={})=>{
 const body=document?.body;
 if(!body)return false;
 const next=Boolean(collapsed);
 body.classList.toggle(collapsedClass,next);
 const button=document.querySelector?.('#sidebarCollapse');
 if(button){
  const label=next?labels.collapsed:labels.expanded;
  button.textContent=next?'›':'‹';
  button.title=label;
  button.setAttribute('aria-label',label);
  button.setAttribute('aria-expanded',String(!next));
 }
 if(persist)writePreference(storage,next);
 return next;
};
const initialize=(document,storage)=>{
 const body=document?.body;
 const button=document?.querySelector?.('#sidebarCollapse');
 if(!body||!button)return null;
 const saved=readPreference(storage);
 apply(document,saved,{storage});
 button.addEventListener('click',()=>{
  apply(document,!body.classList.contains(collapsedClass),{storage,persist:true});
 });
 return {collapsed:saved,button};
};

root.CRMSidebarState={
 STORAGE_KEY,
 readPreference,
 writePreference,
 apply,
 initialize,
};
})(typeof window!=='undefined'?window:globalThis);
