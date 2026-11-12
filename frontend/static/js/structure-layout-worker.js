/* Background geometry only; facts and personal state stay in the current scene. */
import {computeStructureLayout} from './structure-layout-protocol.js';

self.onmessage=event=>{
    const {generation,structureToken,signature}=event.data||{};
    self.postMessage({kind:'started',generation,structureToken,signature});
    self.postMessage(computeStructureLayout(event.data));
};
