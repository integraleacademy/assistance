(function(root){
'use strict';

const STORAGE_KEY='crm-sidebar-collapsed';
const collapsedClass='sidebar-collapsed';
const mobileOpenClass='sidebar-mobile-open';
const labels={
 collapsed:'Déplier la barre latérale',
 expanded:'Replier la barre latérale',
 mobileOpen:'Fermer le menu',
 mobileClosed:'Ouvrir le menu',
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
const applyMobile=(document,open)=>{
 const body=document?.body;
 const sidebar=document?.querySelector?.('#crmSidebar')||document?.querySelector?.('.sidebar');
 const button=document?.querySelector?.('#menuToggle');
 const backdrop=document?.querySelector?.('#sidebarBackdrop');
 if(!body||!sidebar||!button)return false;
 const next=Boolean(open);
 body.classList.toggle(mobileOpenClass,next);
 sidebar.classList.toggle('open',next);
 button.setAttribute('aria-expanded',String(next));
 button.setAttribute('aria-label',next?labels.mobileOpen:labels.mobileClosed);
 if(backdrop){
  backdrop.hidden=!next;
  backdrop.setAttribute('aria-hidden',String(!next));
 }
 return next;
};
const initialize=(document,storage)=>{
 const body=document?.body;
 if(!body)return null;
 const button=document.querySelector?.('#sidebarCollapse');
 const saved=readPreference(storage);
 apply(document,saved,{storage});
 button?.addEventListener('click',()=>{
  apply(document,!body.classList.contains(collapsedClass),{storage,persist:true});
 });

 const menuButton=document.querySelector?.('#menuToggle');
 const sidebar=document.querySelector?.('#crmSidebar')||document.querySelector?.('.sidebar');
 const backdrop=document.querySelector?.('#sidebarBackdrop');
 const closeMobile=()=>applyMobile(document,false);
 if(menuButton&&sidebar){
  applyMobile(document,false);
  menuButton.addEventListener('click',()=>applyMobile(document,!sidebar.classList.contains('open')));
  backdrop?.addEventListener('click',closeMobile);
  sidebar.querySelectorAll?.('a[data-nav]').forEach(link=>link.addEventListener('click',closeMobile));
  document.addEventListener?.('keydown',event=>{if(event.key==='Escape')closeMobile()});
  const viewport=root.matchMedia?.('(max-width:1000px)');
  viewport?.addEventListener?.('change',event=>{if(!event.matches)closeMobile()});
 }
 return {collapsed:saved,button,menuButton,closeMobile};
};

root.CRMSidebarState={
 STORAGE_KEY,
 readPreference,
 writePreference,
 apply,
 applyMobile,
 initialize,
};
})(typeof window!=='undefined'?window:globalThis);
