(function(){
  const stage=document.getElementById('creatorGlobe'); if(!stage) return;
  const track=stage.querySelector('.globe-cards');
  const cards=[...stage.querySelectorAll('.globe-card')];
  let angle=0,startX=0,dragging=false,last=performance.now(),paused=false;
  const radius=178;
  function render(){
    cards.forEach((card,i)=>{
      const a=(angle+i*(360/cards.length))*Math.PI/180;
      const x=Math.sin(a)*radius;
      const z=Math.cos(a)*radius;
      const scale=.88+((z+radius)/(2*radius))*.14;
      const opacity=.68+((z+radius)/(2*radius))*.32;
      card.style.transform=`translate(-50%,-50%) translate3d(${x}px,0,${z}px) scale(${scale})`;
      card.style.opacity=opacity.toFixed(2);
      card.style.zIndex=String(Math.round(z+radius+10));
    });
  }
  function tick(now){
    const reduced=window.matchMedia('(prefers-reduced-motion: reduce)').matches;
    if(!paused&&!dragging&&!reduced) angle=(angle+(now-last)*0.006)%360;
    last=now; render(); requestAnimationFrame(tick);
  }
  stage.addEventListener('pointerdown',e=>{dragging=true;startX=e.clientX;stage.setPointerCapture(e.pointerId)});
  stage.addEventListener('pointermove',e=>{if(!dragging)return;angle+=(e.clientX-startX)*.28;startX=e.clientX;render()});
  stage.addEventListener('pointerup',()=>dragging=false); stage.addEventListener('pointercancel',()=>dragging=false);
  stage.addEventListener('mouseenter',()=>paused=true); stage.addEventListener('mouseleave',()=>paused=false);
  stage.addEventListener('wheel',e=>{e.preventDefault();angle+=(e.deltaY>0?-18:18);render()},{passive:false});
  document.querySelectorAll('[data-globe]').forEach(b=>b.addEventListener('click',()=>{angle+=b.dataset.globe==='right'?-120:120;render()}));
  stage.addEventListener('keydown',e=>{if(e.key==='ArrowRight'){angle-=120;render()} if(e.key==='ArrowLeft'){angle+=120;render()}});
  render(); requestAnimationFrame(tick);
})();
