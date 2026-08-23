(function(root){
'use strict';

const DEFAULT_TITLE='Intégrale Connect CRM';
const SECTION_LABELS=Object.freeze({
 accueil:'Accueil',
 'fil-actu':'Fil d’actualité',
 calendrier:'Calendrier',
 contacts:'Contacts',
 pistes:'Pistes',
 relances:'Relances',
 inscrits:'Inscrits',
 disqualifies:'Disqualifiés',
 notifications:'Notifications',
 modeles:'Modèles',
 exports:'Exports',
});
const formatFirstName=value=>String(value||'').trim().replace(/\s+/g,' ').toLocaleLowerCase('fr-FR').replace(/^clement$/u,'clément').replace(/(^|[\s'-])\p{L}/gu,match=>match.toLocaleUpperCase('fr-FR'));
const formatLastName=value=>String(value||'').trim().replace(/\s+/g,' ').toLocaleUpperCase('fr-FR');
const displayName=contact=>`${formatFirstName(contact?.prenom)} ${formatLastName(contact?.nom)}`.trim();
const sectionLabel=(section,serverLabel='')=>String(SECTION_LABELS[section]||serverLabel||'').trim();
const titleForSection=(section,serverLabel='')=>{
 const label=sectionLabel(section,serverLabel);
 return label?`${label} - Intégrale CRM`:DEFAULT_TITLE;
};
const applySection=(section,serverLabel='')=>{
 const title=titleForSection(section,serverLabel);
 root.document.title=title;
 return title;
};
const titleForContact=(contact,fallbackSection='',fallbackLabel='')=>{
 const name=displayName(contact);
 return name?`${name} - Intégrale CRM`:titleForSection(fallbackSection,fallbackLabel);
};
const applyContact=(contact,fallbackSection='',fallbackLabel='')=>{
 const title=titleForContact(contact,fallbackSection,fallbackLabel);
 root.document.title=title;
 return title;
};
const reset=()=>{
 root.document.title=DEFAULT_TITLE;
 return DEFAULT_TITLE;
};

root.CRMDocumentTitle={DEFAULT_TITLE,SECTION_LABELS,formatFirstName,formatLastName,displayName,sectionLabel,titleForSection,applySection,titleForContact,applyContact,reset};
})(typeof window!=='undefined'?window:globalThis);
