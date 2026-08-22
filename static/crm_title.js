(function(root){
'use strict';

const DEFAULT_TITLE='Intégrale Connect CRM';
const formatFirstName=value=>String(value||'').trim().toLocaleLowerCase('fr-FR').replace(/^clement$/u,'clément').replace(/(^|[\s'-])\p{L}/gu,match=>match.toLocaleUpperCase('fr-FR'));
const formatLastName=value=>String(value||'').trim().toLocaleUpperCase('fr-FR');
const displayName=contact=>`${formatFirstName(contact?.prenom)} ${formatLastName(contact?.nom)}`.trim();
const titleForContact=contact=>{
 const name=displayName(contact);
 return name?`${name} - Intégrale CRM`:DEFAULT_TITLE;
};
const applyContact=contact=>{
 const title=titleForContact(contact);
 root.document.title=title;
 return title;
};
const reset=()=>{
 root.document.title=DEFAULT_TITLE;
 return DEFAULT_TITLE;
};

root.CRMDocumentTitle={DEFAULT_TITLE,formatFirstName,formatLastName,displayName,titleForContact,applyContact,reset};
})(typeof window!=='undefined'?window:globalThis);
